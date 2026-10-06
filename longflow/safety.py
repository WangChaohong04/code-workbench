"""Model-independent, SQLite-backed cooperative safety controls.

Only trusted control-plane code may call confirmation/freeze/recovery methods.
``actor`` and ``actor_role`` MUST come from authenticated server context, never
chat or tool arguments. These checks do not sandbox Python plugins or constitute
an OS security boundary. Tool authorization is a preflight, not an atomic fence
around external I/O: the runtime must recheck immediately before dispatch and
must treat already-running side effects as potentially in doubt after a freeze.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping

from . import db
from .permissions import PermissionDenied
from .redaction import redact_obj


# Reviewed server-side registry, never populated from plugin declarations/chat.
CONTROLLED_TOOLS = frozenset({'kb_search', 'record_note'})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS safety_control (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  frozen INTEGER NOT NULL DEFAULT 0,
  epoch INTEGER NOT NULL DEFAULT 0,
  actor TEXT NOT NULL DEFAULT 'system', reason TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS safety_contracts (
  root_id TEXT PRIMARY KEY, goal TEXT NOT NULL, owner TEXT NOT NULL,
  workspace TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'read_only',
  confidence TEXT NOT NULL DEFAULT 'low', confirmed_by TEXT,
  allowed_tools_json TEXT NOT NULL DEFAULT '[]', scope TEXT NOT NULL DEFAULT '',
  constraints_json TEXT NOT NULL DEFAULT '[]', resources_json TEXT NOT NULL DEFAULT '{}',
  denied_tools_json TEXT NOT NULL DEFAULT '[]', engineering_json TEXT NOT NULL DEFAULT '{}',
  revision INTEGER NOT NULL DEFAULT 0,
  confirmed_epoch INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS safety_progress (
  root_id TEXT PRIMARY KEY,
  execution_status TEXT NOT NULL DEFAULT 'not_started',
  verification_status TEXT NOT NULL DEFAULT 'not_started',
  code_recovery TEXT NOT NULL DEFAULT 'not_requested',
  config_recovery TEXT NOT NULL DEFAULT 'not_requested',
  data_recovery TEXT NOT NULL DEFAULT 'not_requested',
  updated_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS safety_events_no_update
BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS safety_events_no_delete
BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


def init_schema(conn) -> None:
    """Idempotent migration; call from db.init_db AFTER base schema creation."""
    conn.executescript(_SCHEMA)
    # Additive migration for databases created before extended contract fields.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(safety_contracts)")}
    for name, decl in (("resources_json", "TEXT NOT NULL DEFAULT '{}'"),
                       ("denied_tools_json", "TEXT NOT NULL DEFAULT '[]'"),
                       ("engineering_json", "TEXT NOT NULL DEFAULT '{}'")):
        if name not in existing:
            conn.execute(f"ALTER TABLE safety_contracts ADD COLUMN {name} {decl}")
    conn.execute("INSERT OR IGNORE INTO safety_control(singleton,updated_at) VALUES(1,?)", (db.now(),))
    conn.commit()


def _audit(conn, kind, *, root_id=None, actor='runtime', detail=None):
    # Unlike events.emit, do not commit halfway through a control transaction.
    conn.execute(
        "INSERT INTO events(ts,task_id,kind,actor,detail_json) VALUES(?,?,?,?,?)",
        (db.now(), root_id, kind, actor, json.dumps(redact_obj(detail or {}), ensure_ascii=False)),
    )


def _user(actor, actor_role):
    if actor_role != 'user' or not isinstance(actor, str) or not actor.strip():
        raise PermissionDenied('Safety changes require an authenticated user control action')


def _control(conn):
    return dict(conn.execute('SELECT * FROM safety_control WHERE singleton=1').fetchone())


def ensure_contract(conn, root_id, goal, *, owner='local', workspace='default') -> dict:
    """Create a read-only contract. Repeated calls never overwrite confirmation."""
    if not all(isinstance(x, str) and x.strip() for x in (root_id, owner, workspace)):
        raise ValueError('root_id, owner and workspace must be nonempty strings')
    with conn:
        cur = conn.execute(
            '''INSERT OR IGNORE INTO safety_contracts
               (root_id,goal,owner,workspace,created_at,updated_at) VALUES(?,?,?,?,?,?)''',
            (root_id, goal, owner, workspace, db.now(), db.now()),
        )
        contract = get_contract(conn, root_id)
        if contract['owner'] != owner or contract['workspace'] != workspace:
            raise PermissionDenied('Contract ownership/workspace mismatch')
        conn.execute('INSERT OR IGNORE INTO safety_progress(root_id,updated_at) VALUES(?,?)',
                     (root_id, db.now()))
        if cur.rowcount:
            _audit(conn, 'safety_contract_created', root_id=root_id,
                   detail={'mode': 'read_only', 'confidence': 'low'})
    return contract


def get_contract(conn, root_id) -> dict | None:
    row = conn.execute('SELECT * FROM safety_contracts WHERE root_id=?', (root_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result['allowed_tools'] = json.loads(result.pop('allowed_tools_json'))
    result['constraints'] = json.loads(result.pop('constraints_json'))
    result['engineering'] = json.loads(result.pop('engineering_json'))
    result['resources'] = json.loads(result.pop('resources_json'))
    result['denied_tools'] = json.loads(result.pop('denied_tools_json'))
    return result


def confirm_contract(conn, root_id, *, actor, actor_role='user', allowed_tools=(),
                     confidence='medium', scope='', constraints=(), resources=None,
                     denied_tools=(), engineering=None) -> dict:
    """Replace explicit tool grants, never infer them from goal/chat/model text.

    Low/medium confidence preserves read-only mode and empty effective grants.
    High confidence plus concrete tool names is required for write mode. Scope
    and constraints are human-visible descriptions, NOT executable ACLs; tools
    must also pass the runtime's parameter-bound approvals/permission checks.
    """
    _user(actor, actor_role)
    if confidence not in ('low', 'medium', 'high'):
        raise ValueError('confidence must be low, medium or high')
    if isinstance(allowed_tools, (str, bytes)) or not isinstance(allowed_tools, (list, tuple)):
        raise ValueError('allowed_tools must be a list/tuple of exact tool names')
    tools = sorted(set(allowed_tools))
    if any(not isinstance(t, str) or not re.fullmatch(r'[\w.:-]+', t) for t in tools):
        raise ValueError('allowed_tools must contain exact non-wildcard tool names')
    if isinstance(constraints, (str, bytes)) or not isinstance(constraints, (list, tuple)):
        raise ValueError('constraints must be a list/tuple')
    if any(not isinstance(c, str) for c in constraints) or not isinstance(scope, str):
        raise ValueError('scope and constraints must be text')
    resources = resources or {}
    if not isinstance(resources, dict) or any(
        not isinstance(k, str) or not isinstance(v, (list, tuple))
        or any(not isinstance(x, str) for x in v) for k, v in resources.items()
    ):
        raise ValueError('resources must map argument names to exact allowed values')
    if isinstance(denied_tools, str) or any(not isinstance(t, str) for t in denied_tools):
        raise ValueError('denied_tools must be a list of tool names')
    engineering = engineering or {}
    if 'code_candidate' in tools:
        required = ('repository', 'branch', 'allowed_dirs', 'acceptance', 'required_tests')
        if any(not engineering.get(key) for key in required):
            raise ValueError('code_candidate requires a complete engineering contract')
        if engineering.get('environment') != 'sandbox':
            raise PermissionDenied('code_candidate is limited to sandbox environment')
    fields = {'repository', 'branch', 'modules', 'allowed_dirs', 'forbidden_dirs',
              'allow_dependencies', 'allow_config', 'database', 'environment',
              'acceptance', 'required_tests', 'constraints', 'stop_conditions'}
    if not isinstance(engineering, dict) or set(engineering) - fields:
        raise ValueError('Unknown engineering contract fields')
    if engineering and any(t not in CONTROLLED_TOOLS | {'code_candidate'} for t in tools):
        raise PermissionDenied('Non-engineering tools are forbidden by engineering contracts')
    with conn:
        # Acquire SQLite write reservation before reading the current epoch.
        conn.execute('UPDATE safety_control SET epoch=epoch WHERE singleton=1')
        contract = get_contract(conn, root_id)
        if contract is None or contract['owner'] != actor:
            raise PermissionDenied('Only the contract owner may confirm it')
        control = _control(conn)
        if control['frozen'] and tools:
            raise PermissionDenied('Cannot authorize writes while frozen')
        effective = tools if confidence == 'high' else []
        conn.execute(
            '''UPDATE safety_contracts SET mode=?,confidence=?,confirmed_by=?,
               allowed_tools_json=?,scope=?,constraints_json=?,revision=revision+1,
               confirmed_epoch=?,updated_at=? WHERE root_id=?''',
            ('write' if effective else 'read_only', confidence, actor, json.dumps(effective),
             scope, json.dumps(list(constraints)), control['epoch'], db.now(), root_id),
        )
        conn.execute('''UPDATE safety_contracts SET resources_json=?,denied_tools_json=?,
                        engineering_json=? WHERE root_id=?''',
                     (json.dumps(resources), json.dumps(list(denied_tools)),
                      json.dumps(engineering), root_id))
        # Old approvals cannot survive a revised contract (including restriction).
        conn.execute("""UPDATE approvals SET status='revoked',decided_by=?,decided_at=?
                        WHERE status IN ('pending','approved') AND task_id IN
                        (SELECT id FROM tasks WHERE root_id=?)""", (actor, db.now(), root_id))
        _audit(conn, 'safety_contract_confirmed', root_id=root_id, actor=actor,
               detail={'confidence': confidence, 'allowed_tools': effective,
                       'epoch': control['epoch'], 'revision': contract['revision'] + 1})
    return get_contract(conn, root_id)


def _field(spec, key, default=None):
    return spec.get(key, default) if isinstance(spec, Mapping) else getattr(spec, key, default)


def classify_operation(name, args, spec) -> dict:
    """Conservative effect floor; plugin declarations can raise, never lower it.

    This is metadata/argument analysis, not proof of handler behavior. Unknown
    operations are high risk; generic code execution is uncontrolled and denied.
    """
    name = str(name).lower()
    args = args if isinstance(args, Mapping) else {}
    tokens = set(re.findall(r'[a-z]+', name))
    effect_text = ' '.join(str(args.get(k, '')).lower() for k in
                           ('action', 'operation', 'method', 'command', 'sql', 'mode'))
    effects = set(re.findall(r'[a-z]+', effect_text))
    declared = _field(spec, 'risk', 'high')
    # Registration/manifest claims alone never establish containment.
    controlled = name in CONTROLLED_TOOLS and spec is not None and _field(spec, 'name') == name
    dangerous = {'exec', 'execute', 'shell', 'powershell', 'bash', 'eval', 'python', 'subprocess'}
    if tokens & dangerous or any(k in args for k in ('command', 'script', 'code', 'sql')):
        controlled = False
    high = {'delete', 'remove', 'drop', 'truncate', 'destroy', 'send', 'publish', 'upload',
            'purchase', 'pay', 'payment', 'transfer', 'deploy', 'install', 'uninstall',
            'chmod', 'grant', 'revoke', 'approve', 'restore', 'rollback', 'reset', 'merge', 'production'}
    writes = {'write', 'update', 'create', 'insert', 'put', 'post', 'patch', 'save', 'edit',
              'append', 'set', 'record', 'notify', 'notification', 'commit',
              'dependency', 'dependencies', 'auth', 'config', 'refactor', 'migration'}
    reads = {'read', 'get', 'list', 'search', 'query', 'inspect', 'fetch', 'lookup', 'status'}
    if not controlled or (tokens | effects) & high:
        risk, reason = 'high', 'destructive/external/security-sensitive or uncontrolled operation'
    elif (tokens | effects) & writes or _field(spec, 'side_effect', False):
        risk, reason = 'medium', 'persistent mutation or declared side effect'
    elif tokens & reads and (not effects or effects <= {'get', 'head', 'read', 'readonly'}):
        risk, reason = 'low', 'recognized read operation'
    else:
        risk, reason = 'high', 'unknown effects require conservative handling'
    ranks = {'low': 0, 'medium': 1, 'high': 2}
    declared = declared if declared in ranks else 'high'
    if ranks[declared] > ranks[risk]:
        risk, reason = declared, 'tool declaration raises risk floor'
    return {'risk': risk, 'side_effect': risk != 'low', 'controlled': controlled, 'reason': reason}


def _set_frozen(conn, frozen, *, actor, actor_role, reason):
    _user(actor, actor_role)
    with conn:
        conn.execute('''UPDATE safety_control SET frozen=?,epoch=epoch+1,actor=?,reason=?,
                        updated_at=? WHERE singleton=1''', (int(frozen), actor, reason, db.now()))
        cur = conn.execute("""UPDATE approvals SET status='revoked',decided_by=?,decided_at=?
                              WHERE status IN ('pending','approved')""", (actor, db.now()))
        control = _control(conn)
        _audit(conn, 'safety_frozen' if frozen else 'safety_unfrozen', actor=actor,
               detail={'epoch': control['epoch'], 'reason': reason, 'revoked': cur.rowcount,
                       'running_operations': 'not_safely_interruptible'})
    return {**control, 'frozen': bool(control['frozen']),
            'running_operations': 'not_safely_interruptible'}


def freeze(conn, *, actor, actor_role='user', reason='') -> dict:
    """Block new writes globally; does NOT cancel/undo already-running effects."""
    return _set_frozen(conn, True, actor=actor, actor_role=actor_role, reason=reason)


def unfreeze(conn, *, actor, actor_role='user', reason='') -> dict:
    """Resume preflight only; old contracts/approvals never regain write authority."""
    return _set_frozen(conn, False, actor=actor, actor_role=actor_role, reason=reason)


def get_epoch(conn) -> int:
    return int(_control(conn)['epoch'])


def is_frozen(conn) -> bool:
    return bool(_control(conn)['frozen'])


def check_tool(conn, task, name, args, spec) -> dict:
    """Check contract/freeze/control floor; does not replace per-call approvals.

    Return effective risk for permissions.decide. Task may be a models.Task or
    mapping; trusted persisted root owner/workspace, not task input, bind grants.
    """
    operation = classify_operation(name, args, spec)
    root_id = _field(task, 'root_id')
    contract = get_contract(conn, root_id)
    reason = None
    control = _control(conn)
    if contract and name in contract['denied_tools']:
        reason = 'Tool explicitly denied by contract'
    elif not operation['controlled']:
        reason = 'Tool is uncontrolled; arbitrary execution is not permitted'
    elif operation['side_effect']:
        row = conn.execute('SELECT owner,workspace FROM tasks WHERE id=?', (root_id,)).fetchone()
        if control['frozen']:
            reason = 'Safety freeze blocks new writes'
        elif contract is None or row is None:
            reason = 'A persisted task contract is required for writes'
        elif contract['owner'] != row['owner'] or contract['workspace'] != row['workspace']:
            reason = 'Contract ownership/workspace mismatch'
        elif contract['confidence'] != 'high' or contract['mode'] != 'write':
            reason = 'Low/medium confidence or read-only contract forbids writes'
        elif contract['confirmed_by'] != contract['owner']:
            reason = 'Write contract lacks owner confirmation'
        elif contract['confirmed_epoch'] != control['epoch']:
            reason = 'Write contract is stale after a safety epoch change; reconfirm required'
        elif name not in contract['allowed_tools']:
            reason = 'Tool is outside the explicitly confirmed contract'
        else:
            resource_keys = {'path', 'file', 'directory', 'url', 'object', 'repository',
                             'branch', 'database', 'environment', 'to', 'channel', 'vendor'}
            for key in resource_keys | set(contract['resources']):
                if key in args and args[key] not in contract['resources'].get(key, []):
                    reason = 'Resource is outside explicitly confirmed exact values: ' + key
                    break
    if reason:
        with conn:
            _audit(conn, 'safety_tool_denied', root_id=root_id,
                   detail={'tool': name, 'reason': reason, 'risk': operation['risk'],
                           'epoch': control['epoch']})
        raise PermissionDenied(reason)
    return {**operation, 'epoch': control['epoch']}


def _progress(conn, root_id, column, status, *, actor, detail):
    if get_contract(conn, root_id) is None:
        raise ValueError('Unknown contract')
    with conn:
        conn.execute(f'UPDATE safety_progress SET {column}=?,updated_at=? WHERE root_id=?',
                     (status, db.now(), root_id))
        _audit(conn, 'safety_progress', root_id=root_id, actor=actor,
               detail={'dimension': column, 'status': status, 'detail': detail})
    return snapshot(conn, root_id)


def record_execution(conn, root_id, status, *, actor='runtime', detail='') -> dict:
    """Trusted runtime reports execution only, never verification or recovery."""
    if status not in ('not_started', 'running', 'succeeded', 'failed', 'in_doubt', 'cancelled'):
        raise ValueError('Invalid execution status')
    return _progress(conn, root_id, 'execution_status', status, actor=actor, detail=detail)


def record_verification(conn, root_id, status, *, actor='runtime', detail='') -> dict:
    """Trusted verifier reports evidence outcome independently from execution."""
    if status not in ('not_started', 'running', 'passed', 'failed', 'inconclusive'):
        raise ValueError('Invalid verification status')
    return _progress(conn, root_id, 'verification_status', status, actor=actor, detail=detail)


def record_recovery(conn, root_id, plane, status, *, actor, actor_role='user', detail='') -> dict:
    """Record, never perform, independently verified code/config/data recovery.

    This deliberately exposes no chat-driven update, deployment, or self-update
    pathway. Recovering code does not assert data/config restoration or success.
    """
    _user(actor, actor_role)
    contract = get_contract(conn, root_id)
    if contract is None or contract['owner'] != actor:
        raise PermissionDenied('Only the owner may record recovery')
    if plane not in ('code', 'config', 'data'):
        raise ValueError('Recovery plane must be code, config or data')
    if status not in ('not_requested', 'requested', 'running', 'failed', 'unavailable'):
        raise ValueError('Invalid recovery status')
    return _progress(conn, root_id, plane + '_recovery', status, actor=actor, detail=detail)


def record_recovery_verified(conn, root_id, plane, *, actor, actor_role, evidence) -> dict:
    """Trusted independent verifier only; caller authenticates this service role.

    Evidence identifies a passing check and durable artifact. Never expose this
    function or its actor_role through user/chat parameters.
    """
    if actor_role != 'verifier' or not isinstance(actor, str) or not actor.strip():
        raise PermissionDenied('Independent verifier required')
    if plane not in ('code', 'config', 'data'):
        raise ValueError('Invalid recovery plane')
    if not isinstance(evidence, dict) or evidence.get('passed') is not True or not all(
        isinstance(evidence.get(k), str) and evidence[k].strip() for k in ('check', 'artifact')
    ):
        raise ValueError('Passing verification evidence with check/artifact required')
    return _progress(conn, root_id, plane + '_recovery', 'succeeded', actor=actor,
                     detail=evidence)


def snapshot(conn, root_id) -> dict:
    """Read-only safety view; statuses are records, not proof or rollback actions."""
    control = _control(conn)
    progress = conn.execute('SELECT * FROM safety_progress WHERE root_id=?', (root_id,)).fetchone()
    return {'contract': get_contract(conn, root_id),
            'control': {**control, 'frozen': bool(control['frozen'])},
            'progress': dict(progress) if progress else None,
            'running_operations': 'not_safely_interruptible',
            'boundary': 'cooperative_in_process_not_os_sandbox',
            'chat_updates_allowed': False}
