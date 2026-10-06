"""Local, user-selected workspaces with exact single-use write approval."""
from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

from .execution_sandbox import ExecutionSandboxService, PRESETS, SandboxConfig

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from . import events, safety
from .local_workspace import LocalWorkspace


class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Changes(StrictBody):
    changes: dict[str, str | None]


class Approval(StrictBody):
    candidate_id: str
    source_hash: str
    candidate_hash: str
    confirmed: StrictBool = False


class Apply(StrictBody):
    approval_id: StrictStr


class ExecutionApproval(Approval):
    preset: StrictStr


class RecordApplied(StrictBody):
    node_id: StrictStr
    confirmed: StrictBool = False


class Restore(StrictBody):
    commit: str


class GoalRequest(StrictBody):
    goal: StrictStr
    confirmed_send_code: StrictBool = False


class ProposalRequest(GoalRequest):
    allowed_paths: list[StrictStr]
    sensitive_paths: list[StrictStr] = Field(default_factory=list)
    confirmed_sensitive: StrictBool = False


class RunCreate(GoalRequest):
    maxsteps: StrictInt = Field(default=100, ge=1, le=1000)
    modelcalls: StrictInt = Field(default=60, ge=1, le=1000)
    repairrounds: StrictInt = Field(default=2, ge=0, le=10)


class RunConfirm(StrictBody):
    confirmed: StrictBool = False


class RunPlanConfirm(RunConfirm):
    allowed_paths: list[StrictStr]


class RunStep(StrictBody):
    confirmed_send_code: StrictBool = False


class RunRetry(RunConfirm):
    node_id: StrictStr


def pick_directory():
    # Executed on the local service host, never from a submitted path.
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    try:
        return filedialog.askdirectory(parent=root, title='选择工程文件夹（读取并创建独立版本记录）')
    finally:
        root.destroy()


def install(app, state, require_principal):
    conn = state.conn
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS local_workspaces (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, workspace TEXT NOT NULL,
      source TEXT NOT NULL, storage TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS local_write_approvals (
      id TEXT PRIMARY KEY, project TEXT NOT NULL, owner TEXT NOT NULL,
      workspace TEXT NOT NULL, candidate_id TEXT NOT NULL,
      source_hash TEXT NOT NULL, candidate_hash TEXT NOT NULL,
      epoch INTEGER NOT NULL, expires_at INTEGER NOT NULL, consumed INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS local_execution_approvals (
      id TEXT PRIMARY KEY, project TEXT NOT NULL, owner TEXT NOT NULL,
      workspace TEXT NOT NULL, candidate_id TEXT NOT NULL, source_hash TEXT NOT NULL,
      candidate_hash TEXT NOT NULL, preset TEXT NOT NULL, epoch INTEGER NOT NULL,
      expires_at INTEGER NOT NULL, consumed INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS local_execution_evidence (
      id TEXT PRIMARY KEY, approval_id TEXT NOT NULL UNIQUE, project TEXT NOT NULL,
      owner TEXT NOT NULL, workspace TEXT NOT NULL, candidate_id TEXT NOT NULL,
      source_hash TEXT NOT NULL, candidate_hash TEXT NOT NULL, preset TEXT NOT NULL,
      epoch INTEGER NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
      evidence TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS local_application_receipts (
      id TEXT PRIMARY KEY, project TEXT NOT NULL, owner TEXT NOT NULL, workspace TEXT NOT NULL,
      candidate_id TEXT NOT NULL, source_hash TEXT NOT NULL, candidate_hash TEXT NOT NULL,
      commit_id TEXT NOT NULL, applied_source_hash TEXT NOT NULL, epoch INTEGER NOT NULL,
      consumed_run TEXT, consumed_node TEXT);
    ''')
    lock = threading.RLock()
    picker_lock = threading.Lock()

    def sandbox_config():
        raw = state.cfg.get('execution_sandbox') or {}
        if not isinstance(raw, dict):
            raw = {}
        allowed = {k: raw[k] for k in SandboxConfig.__dataclass_fields__ if k in raw}
        return SandboxConfig(**allowed)

    def sandbox_available():
        cfg = sandbox_config()
        if cfg.enabled is not True:
            return False
        try:
            cfg.validate()
        except (ValueError, TypeError):
            return False
        return bool(shutil.which('docker'))

    def execution_capabilities():
        cfg = sandbox_config()
        valid = False
        if cfg.enabled is True:
            try:
                cfg.validate()
                valid = True
            except (ValueError, TypeError):
                pass
        docker = bool(shutil.which('docker'))
        return {'available': valid and docker, 'enabled': cfg.enabled is True,
                'configured': valid, 'presets': sorted(PRESETS),
                'daemon_verified': False, 'approval_required': True,
                'reason': 'invalid_configuration' if type(cfg.enabled) is not bool else 'disabled' if cfg.enabled is False else (
                    'invalid_configuration' if not valid else (
                        'docker_cli_missing' if not docker else 'daemon_not_verified'))}

    def sandbox_for(s):
        return ExecutionSandboxService(s, config=sandbox_config())

    def local(request):
        host = request.client.host if request.client else ''
        if host not in ('127.0.0.1', '::1', 'localhost', 'testclient'):
            raise HTTPException(403, '本机工程接口仅允许 loopback 访问')
        origin = request.headers.get('origin')
        if origin:
            from urllib.parse import urlsplit
            target = urlsplit(origin)
            if target.scheme not in ('http', 'https') or target.netloc != request.headers.get('host'):
                raise HTTPException(403, '跨站请求被拒绝')
        if request.headers.get('sec-fetch-site') == 'cross-site':
            raise HTTPException(403, '跨站请求被拒绝')

    def load(wid, principal):
        row = conn.execute('SELECT * FROM local_workspaces WHERE id=? AND owner=? AND workspace=?',
                           (wid, principal.user, principal.workspace)).fetchone()
        if not row:
            raise HTTPException(404, '工作区不存在或无权限')
        return LocalWorkspace(row['source'], row['storage'])

    def check_write():
        if safety.is_frozen(conn):
            raise HTTPException(409, 'global_freeze：禁止新的文件写入')

    def invoke(fn):
        try:
            return fn()
        except HTTPException:
            raise
        except (ValueError, OSError, RuntimeError) as exc:
            raise HTTPException(409, {'code': getattr(exc, 'code', 'workspace_error'), 'error': str(exc)}) from exc

    def ai_service():
        # Runtime-local import keeps the local router importable during staged rollouts.
        from .engineering_ai import EngineeringAI
        return EngineeringAI(state.engine().driver)

    def ai_request(body):
        if not body.goal.strip():
            raise HTTPException(422, '目标不能为空')
        if not body.confirmed_send_code:
            raise HTTPException(422, '必须明确同意向模型发送代码上下文')

    def ai_snapshot(wid, principal):
        s = load(wid, principal)
        return s, safety.get_epoch(conn), s.info()['source_hash']

    def ai_recheck(wid, principal, epoch, source_hash):
        if safety.get_epoch(conn) != epoch or safety.is_frozen(conn):
            raise HTTPException(409, '工作区在模型调用期间被冻结，结果已丢弃')
        current = load(wid, principal).info()['source_hash']
        if current != source_hash:
            raise HTTPException(409, '源目录在模型调用期间变化，结果已丢弃')

    def ai_invoke(fn):
        try:
            return fn()
        except HTTPException:
            raise
        except Exception as exc:
            from .engineering_ai import AIUnavailable
            if isinstance(exc, AIUnavailable):
                raise HTTPException(409, {'code': 'ai_unavailable', 'error': str(exc)}) from exc
            if isinstance(exc, (ValueError, OSError, RuntimeError)):
                raise HTTPException(409, {'code': getattr(exc, 'code', 'ai_error'), 'error': str(exc)}) from exc
            raise

    @app.post('/api/local-workspaces/{wid}/ai/analyze')
    def ai_analyze(wid: str, body: GoalRequest, request: Request, principal=Depends(require_principal)):
        local(request)
        ai_request(body)
        with lock:
            s, epoch, source_hash = ai_snapshot(wid, principal)
        result = ai_invoke(lambda: ai_service().analyze(s, body.goal))
        with lock:
            ai_recheck(wid, principal, epoch, source_hash)
            events.emit(conn, 'local_ai_analyzed', actor=principal.user,
                        detail={'workspace_id': wid, 'source_hash': source_hash,
                                'evidence': result.get('evidence', {})})
        return result

    @app.post('/api/local-workspaces/{wid}/ai/plan')
    def ai_plan(wid: str, body: GoalRequest, request: Request, principal=Depends(require_principal)):
        local(request)
        ai_request(body)
        with lock:
            s, epoch, source_hash = ai_snapshot(wid, principal)
        result = ai_invoke(lambda: ai_service().plan(s, body.goal))
        with lock:
            ai_recheck(wid, principal, epoch, source_hash)
            events.emit(conn, 'local_ai_planned', actor=principal.user,
                        detail={'workspace_id': wid, 'source_hash': source_hash,
                                'evidence': result.get('evidence', {})})
        return result

    @app.post('/api/local-workspaces/{wid}/ai/propose')
    def ai_propose(wid: str, body: ProposalRequest, request: Request, principal=Depends(require_principal)):
        local(request)
        ai_request(body)
        if body.sensitive_paths and not body.confirmed_sensitive:
            raise HTTPException(422, '敏感路径需要单独明确确认')
        with lock:
            check_write()
            s, epoch, source_hash = ai_snapshot(wid, principal)
        scope = {'paths': list(body.allowed_paths), 'sensitive_paths': list(body.sensitive_paths)}
        def authorize_candidate_write():
            with lock:
                ai_recheck(wid, principal, epoch, source_hash)
        result = ai_invoke(lambda: ai_service().propose(s, body.goal, scope, check_write=authorize_candidate_write))
        with lock:
            # Candidate creation is staged only; discard the late result if safety changed.
            ai_recheck(wid, principal, epoch, source_hash)
            events.emit(conn, 'local_ai_proposed', actor=principal.user,
                        detail={'workspace_id': wid, 'source_hash': source_hash,
                                'candidate_id': result.get('candidate_id'),
                                'candidate_hash': result.get('candidate_hash'),
                                'evidence': result.get('ai_evidence', {})})
        return result

    @app.get('/api/local-workspaces/{wid}/capabilities')
    def ai_capabilities(wid: str, request: Request, principal=Depends(require_principal)):
        local(request)
        with lock:
            load(wid, principal)
            configured = state.cfg.get('llm', {})
            try:
                effective = getattr(state.engine().driver, 'name', 'unknown')
            except Exception:
                effective = 'unavailable'
            return {'ai': {'configured_driver': configured.get('driver', 'local'),
                           'effective_driver': effective,
                           'model': configured.get('model', ''),
                           'api_key_configured': bool(configured.get('api_key')),
                           'available': effective == 'openai_compatible' and bool(configured.get('api_key'))},
                    'execution': execution_capabilities(),
                    'docker': {'configured': bool(sandbox_config().enabled),
                                'available': bool(shutil.which('docker'))}}

    run_service = None

    def runs():
        nonlocal run_service
        # Lazy schema creation: importing/installing this router does not migrate runs.
        if run_service is None:
            from .engineering_runs import EngineeringRunService
            run_service = EngineeringRunService(conn)
            conn.execute('CREATE TABLE IF NOT EXISTS local_engineering_run_plans ('
                         'run_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, '
                         'owner TEXT NOT NULL, workspace TEXT NOT NULL, plan TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS local_engineering_run_scopes ('
                         'run_id TEXT PRIMARY KEY, allowed_paths TEXT NOT NULL)')
            conn.commit()
        return run_service

    def run_binding(wid, principal):
        s, epoch, source_hash = ai_snapshot(wid, principal)
        return s, {'workspace_id': wid,
                   'owner': json.dumps([principal.user, principal.workspace]),
                   'epoch': epoch, 'source_hash': source_hash}

    def run_plan(wid, rid, principal):
        row = conn.execute('SELECT plan FROM local_engineering_run_plans WHERE '
                           'run_id=? AND workspace_id=? AND owner=? AND workspace=?',
                           (rid, wid, principal.user, principal.workspace)).fetchone()
        if not row:
            raise HTTPException(404, 'Run does not exist or is not owned by this workspace')
        return json.loads(row['plan'])

    def run_response(result, plan):
        return {**result, 'plan': plan}

    def run_get(wid, rid, principal):
        service = runs()
        plan = run_plan(wid, rid, principal)
        s, binding = run_binding(wid, principal)
        result = invoke(lambda: service.get(rid, **binding))
        return service, s, binding, plan, result

    @app.get('/api/local-workspaces/{wid}/runs')
    def run_capabilities(wid: str, request: Request, principal=Depends(require_principal)):
        local(request)
        with lock:
            load(wid, principal)
            return {'advancement': 'manual', 'background_worker': False,
                    'plan_confirmation_required': True,
                    'execution': execution_capabilities(),
                    'record_applied': {'available': True,
                                       'reason': 'server-derived local workspace receipt'},
                    'auto_apply': False}

    @app.post('/api/local-workspaces/{wid}/runs')
    def run_create(wid: str, body: RunCreate, request: Request, principal=Depends(require_principal)):
        local(request)
        ai_request(body)
        with lock:
            check_write()
            s, binding = run_binding(wid, principal)
        plan = ai_invoke(lambda: ai_service().plan(s, body.goal))
        # Only EngineeringAI's validated inert checks and exact source paths enter
        # the scheduler. The original plan (including acceptance) stays durable.
        nodes = [{'id': task['id'], 'goal': task['objective'],
                  'depends_on': task['deps'], 'tests': ['test', 'lint']}
                 for task in plan['subtasks']]
        with lock:
            ai_recheck(wid, principal, binding['epoch'], binding['source_hash'])
            service = runs()
            result = invoke(lambda: service.create(body.goal, nodes, **binding,
                            maxsteps=body.maxsteps, modelcalls=body.modelcalls,
                            repairrounds=body.repairrounds))
            conn.execute('INSERT INTO local_engineering_run_plans VALUES (?,?,?,?,?)',
                         (result['id'], wid, principal.user, principal.workspace, json.dumps(plan)))
            conn.commit()
            return run_response(result, plan)

    @app.get('/api/local-workspaces/{wid}/runs/{rid}')
    def run_read(wid: str, rid: str, request: Request, principal=Depends(require_principal)):
        local(request)
        with lock:
            _, _, _, plan, result = run_get(wid, rid, principal)
            return run_response(result, plan)

    @app.post('/api/local-workspaces/{wid}/runs/{rid}/confirm')
    def run_confirm(wid: str, rid: str, body: RunPlanConfirm, request: Request,
                    principal=Depends(require_principal)):
        local(request)
        if body.confirmed is not True:
            raise HTTPException(422, 'Explicit plan confirmation required')
        with lock:
            check_write()
            service, _, binding, plan, current = run_get(wid, rid, principal)
            expected = {path for task in plan['subtasks'] for path in task['allowed_paths']}
            if (len(set(body.allowed_paths)) != len(body.allowed_paths) or
                    set(body.allowed_paths) != expected):
                raise HTTPException(422, 'Confirm the exact reviewed plan path scope')
            if current['confirmed'] or current['status'] != 'waiting_user':
                raise HTTPException(409, 'Only a new plan may be confirmed')
            # Persist scope first: a crash before confirmation cannot start work;
            # a confirmed run must never lack durable reviewed permissions.
            conn.execute('INSERT OR REPLACE INTO local_engineering_run_scopes VALUES (?,?)',
                         (rid, json.dumps(sorted(expected))))
            conn.commit()
            return run_response(invoke(lambda: service.confirm(rid, **binding)), plan)

    @app.post('/api/local-workspaces/{wid}/runs/{rid}/step')
    def run_step(wid: str, rid: str, body: RunStep, request: Request,
                 principal=Depends(require_principal)):
        local(request)
        if body.confirmed_send_code is not True:
            raise HTTPException(422, 'Explicit code-context transmission confirmation required')
        with lock:
            check_write()
            service, s, binding, plan, _ = run_get(wid, rid, principal)
            scope_row = conn.execute('SELECT allowed_paths FROM local_engineering_run_scopes '
                                     'WHERE run_id=?', (rid,)).fetchone()
            if not scope_row:
                raise HTTPException(409, 'Explicit reviewed path scope is required')
            reviewed_paths = set(json.loads(scope_row['allowed_paths']))

        def guard():
            with lock:
                ai_recheck(wid, principal, binding['epoch'], binding['source_hash'])
                current = service.get(rid, **binding)
                if current['status'] != 'in_progress':
                    raise HTTPException(409, 'Run attempt was paused or stopped')

        def handle(phase, context):
            guard()
            task = next(t for t in plan['subtasks'] if t['id'] == context['node']['id'])
            goal = task['objective'] + '\nAcceptance:\n' + '\n'.join(task['acceptance'])
            if phase == 'analyze':
                result = ai_service().analyze(s, goal)
                guard()
                return {'status': 'completed', 'evidence': result}
            if phase != 'propose':
                raise ValueError('Unsupported model stage')
            if not set(task['allowed_paths']) <= reviewed_paths:
                raise ValueError('Task paths were not explicitly reviewed')
            allowed_paths = sorted(set(task['allowed_paths']) & reviewed_paths)
            result = ai_service().propose(s, goal, allowed_paths, check_write=guard)
            guard()
            # Re-read staged evidence on the server, never accept model hashes.
            preview = s.preview(result['candidate_id'])
            if (preview['source_hash'] != binding['source_hash'] or
                    not set(preview['changed_files']) <= set(allowed_paths)):
                raise ValueError('Candidate source or scope mismatch')
            guard()
            return {'status': 'completed', 'candidate': {**preview, 'isolated': True,
                    'ai_evidence': result.get('ai_evidence', {})}}

        def finish_guard():
            # Scheduler invokes this under its transaction lock; do not acquire
            # the router lock here (pause/stop acquire the locks in reverse order).
            ai_recheck(wid, principal, binding['epoch'], binding['source_hash'])

        def executor(context):
            node = context['node']
            candidate = node.get('candidate') or {}
            required = {'test': 'pytest', 'lint': 'ruff', 'compile': 'compileall'}
            checks = []
            for check_name in node.get('tests', []):
                preset = required.get(check_name, check_name)
                row = conn.execute('SELECT evidence FROM local_execution_evidence '
                                   'WHERE project=? AND owner=? AND workspace=? AND candidate_id=? '
                                   'AND source_hash=? AND candidate_hash=? AND preset=? AND epoch=? '
                                   'AND expires_at>=? ORDER BY created_at DESC LIMIT 1',
                                   (wid, principal.user, principal.workspace, candidate.get('candidate_id'),
                                    binding['source_hash'], candidate.get('candidate_hash'), preset,
                                    binding['epoch'], int(time.time()))).fetchone()
                evidence = json.loads(row['evidence']) if row else {}
                checks.append({'name': check_name, 'passed': evidence.get('passed') is True,
                               'evidence': evidence if evidence.get('passed') is True else ''})
            return {'checks': checks}

        ready_evidence = False
        with lock:
            state_now = service.get(rid, **binding)
            done = {n['id'] for n in state_now['nodes'] if n['status'] == 'completed'}
            next_node = next((n for n in state_now['nodes'] if n['status'] in ('pending', 'blocked')
                              and set(n['depends_on']) <= done), None)
            if next_node and next_node['phase'] == 'verify':
                ready_evidence = all(c['passed'] for c in executor({'node': next_node})['checks'])
        result = invoke(lambda: service.step(rid, step_handler=handle, executor=executor,
                                             execution_approved=ready_evidence,
                                             safety_guard=finish_guard, **binding))
        with lock:
            ai_recheck(wid, principal, binding['epoch'], binding['source_hash'])
            return run_response(result, plan)

    def run_control(wid, rid, principal, operation, body=None):
        with lock:
            check_write()
            service, _, binding, plan, _ = run_get(wid, rid, principal)
            if operation == 'retry':
                if body.confirmed is not True:
                    raise HTTPException(422, 'Explicit retry confirmation required')
                result = invoke(lambda: service.retry(rid, body.node_id,
                                                      confirmed=True, **binding))
            else:
                result = invoke(lambda: getattr(service, operation)(rid, **binding))
            return run_response(result, plan)

    @app.post('/api/local-workspaces/{wid}/runs/{rid}/pause')
    def run_pause(wid: str, rid: str, body: StrictBody, request: Request,
                  principal=Depends(require_principal)):
        local(request)
        return run_control(wid, rid, principal, 'pause')

    @app.post('/api/local-workspaces/{wid}/runs/{rid}/resume')
    def run_resume(wid: str, rid: str, body: StrictBody, request: Request,
                   principal=Depends(require_principal)):
        local(request)
        return run_control(wid, rid, principal, 'resume')

    @app.post('/api/local-workspaces/{wid}/runs/{rid}/stop')
    def run_stop(wid: str, rid: str, body: StrictBody, request: Request,
                 principal=Depends(require_principal)):
        local(request)
        return run_control(wid, rid, principal, 'stop')

    @app.post('/api/local-workspaces/{wid}/runs/{rid}/record-applied')
    def run_record_applied(wid: str, rid: str, body: RecordApplied, request: Request,
                           principal=Depends(require_principal)):
        local(request)
        if body.confirmed is not True:
            raise HTTPException(422, 'Explicit application confirmation required')
        with lock:
            check_write()
            service = runs()
            plan = run_plan(wid, rid, principal)
            s, binding = run_binding(wid, principal)
            stored = conn.execute('SELECT state FROM engineering_runs WHERE id=?', (rid,)).fetchone()
            if not stored:
                raise HTTPException(404, 'Run not found')
            original = json.loads(stored['state'])
            binding['source_hash'] = original['source_hash']
            current = invoke(lambda: service.get(rid, **binding))
            node = next((n for n in current['nodes'] if n['id'] == body.node_id), None)
            if node is None or node.get('status') != 'waiting_user' or node.get('phase') != 'apply':
                raise HTTPException(409, 'Node is not awaiting application evidence')
            candidate = node.get('candidate') or {}
            candidate_id = candidate.get('candidate_id')
            candidate_hash = candidate.get('candidate_hash')
            source_hash = current['source_hash']
            workspace = load(wid, principal)

        def verify_applied(_context):
            with lock:
                info = workspace.info()
                if info.get('source_hash') != candidate_hash:
                    return {'verified': False}
                receipt = conn.execute('SELECT * FROM local_application_receipts WHERE project=? '
                                       'AND owner=? AND workspace=? AND candidate_id=? AND source_hash=? '
                                       'AND candidate_hash=? AND epoch=? AND consumed_run IS NULL',
                                       (wid, principal.user, principal.workspace, candidate_id,
                                        source_hash, candidate_hash, binding['epoch'])).fetchone()
                versions = workspace.versions()
                match = next((v for v in versions if receipt and v.get('commit') == receipt['commit_id']
                              and v.get('source_hash') == candidate_hash), None)
                if not match or not match.get('commit'):
                    return {'verified': False}
                return {'verified': True, 'candidate_hash': candidate_hash,
                        'source_hash': source_hash,
                        'applied_source_hash': info['source_hash'],
                        'git_evidence': {'commit': match['commit'],
                                         'source_hash': info['source_hash'],
                                         'candidate_id': candidate_id}}

        def application_finish_guard():
            # Called under scheduler CAS lock: avoid router-lock inversion.
            return (not safety.is_frozen(conn)
                    and safety.get_epoch(conn) == binding['epoch']
                    and workspace.info()['source_hash'] == candidate_hash)

        record_binding = dict(binding)
        record_binding.pop('source_hash', None)
        result = invoke(lambda: service.record_applied(rid, body.node_id, candidate_hash, source_hash,
                                                        confirmed=True,
                                                        verify_applied=verify_applied,
                                                        safety_guard=application_finish_guard,
                                                        **record_binding))
        evidence = result['nodes']
        node_result = next(n for n in evidence if n['id'] == body.node_id)
        if node_result.get('status') == 'completed':
            app_ev = node_result.get('application_evidence') or {}
            with lock:
                conn.execute('UPDATE local_application_receipts SET consumed_run=?, consumed_node=? '
                             'WHERE project=? AND owner=? AND workspace=? AND candidate_id=? '
                             'AND source_hash=? AND candidate_hash=? AND commit_id=? AND consumed_run IS NULL',
                             (rid, body.node_id, wid, principal.user, principal.workspace, candidate_id,
                              source_hash, candidate_hash, app_ev.get('git_evidence', {}).get('commit', '')))
                conn.commit()
        return run_response(result, plan)

    @app.post('/api/local-workspaces/{wid}/runs/{rid}/retry')
    def run_retry(wid: str, rid: str, body: RunRetry, request: Request,
                  principal=Depends(require_principal)):
        local(request)
        return run_control(wid, rid, principal, 'retry', body)

    @app.post('/api/local-workspaces/pick')
    def pick(request: Request, principal=Depends(require_principal)):
        local(request)
        if not principal.is_admin:
            raise HTTPException(403, '目录关联需本机管理员权限')
        if not picker_lock.acquire(blocking=False):
            raise HTTPException(409, '已有文件夹选择窗口，请完成或取消')
        try:
            source = pick_directory()
            if not source:
                raise HTTPException(409, '已取消文件夹选择')
            with lock:
                wid = secrets.token_hex(16)
                base = Path(state.cfg['db_path']).resolve().parent / 'local-workspaces' / wid
                base.mkdir(parents=True, exist_ok=False)
                service = invoke(lambda: LocalWorkspace(source, str(base)))
                conn.execute('INSERT INTO local_workspaces VALUES (?,?,?,?,?)',
                             (wid, principal.user, principal.workspace, str(Path(source).resolve()), str(base)))
                conn.commit()
                events.emit(conn, 'local_workspace_selected', actor=principal.user,
                            detail={'workspace_id': wid, 'source': source})
                return {'workspace_id': wid, **service.info(), 'files': [{'path': p} for p in service.list_files()]}
        finally:
            picker_lock.release()

    @app.get('/api/local-workspaces/{wid}')
    def info(wid: str, request: Request, principal=Depends(require_principal)):
        local(request)
        with lock:
            s = invoke(lambda: load(wid, principal))
            return invoke(lambda: {'workspace_id': wid, **s.info(), 'frozen': safety.is_frozen(conn),
                                  'files': [{'path': p} for p in s.list_files()]})

    @app.get('/api/local-workspaces/{wid}/file')
    def read_file(wid: str, path: str, request: Request, principal=Depends(require_principal)):
        local(request)
        with lock:
            return invoke(lambda: {'path': path, 'content': load(wid, principal).read_file(path)})

    @app.post('/api/local-workspaces/{wid}/candidates')
    def candidate(wid: str, body: Changes, request: Request, principal=Depends(require_principal)):
        local(request)
        with lock:
            check_write()
            if not body.changes:
                raise HTTPException(422, '没有暂存文件变更')
            result = invoke(lambda: load(wid, principal).create_candidate(body.changes))
            events.emit(conn, 'local_candidate_created', actor=principal.user,
                        detail={'workspace_id': wid, 'candidate_id': result['candidate_id'],
                                'source_hash': result['source_hash'], 'candidate_hash': result['candidate_hash']})
            return result

    @app.post('/api/local-workspaces/{wid}/restore-preview')
    def restore(wid: str, body: Restore, request: Request, principal=Depends(require_principal)):
        local(request)
        with lock:
            check_write()
            return invoke(lambda: load(wid, principal).restore_candidate(body.commit))

    def _execution_row(wid, aid, principal):
        return conn.execute('SELECT * FROM local_execution_approvals WHERE id=? AND project=? AND owner=? AND workspace=?',
                            (aid, wid, principal.user, principal.workspace)).fetchone()

    def _safe_evidence(value):
        if not isinstance(value, dict):
            return {}
        allowed = {'candidate_id', 'candidate_sha', 'preset', 'image', 'status', 'passed',
                   'exit_code', 'output', 'output_delivered', 'output_truncated',
                   'cleanup_complete', 'reason', 'container_name', 'boundary', 'host_fallback'}
        result = {k: v for k, v in value.items() if k in allowed}
        output = result.get('output')
        if isinstance(output, str):
            output = re.sub(r'(?i)(api[_-]?key|password|secret|token|authorization)(\s*[:=]\s*)[^\s]+',
                            r'\1\2[redacted]', output)
            output = re.sub(r'(?i)bearer\s+[^\s]+', 'Bearer [redacted]', output)
            output = re.sub(r'sk-[A-Za-z0-9_-]{12,}', '[redacted]', output)
            result['output'] = output.encode('utf-8')[:256 * 1024].decode('utf-8', errors='ignore')
        return result

    @app.post('/api/local-workspaces/{wid}/execution/approvals')
    def execution_approve(wid: str, body: ExecutionApproval, request: Request,
                          principal=Depends(require_principal)):
        local(request)
        if body.preset not in PRESETS:
            raise HTTPException(422, 'Unsupported execution preset')
        if not execution_capabilities()['configured']:
            raise HTTPException(409, 'Execution sandbox is disabled or invalidly configured')
        if body.confirmed is not True:
            raise HTTPException(422, 'Explicit execution approval required')
        with lock:
            check_write()
            s = invoke(lambda: load(wid, principal))
            p = invoke(lambda: s.preview(body.candidate_id))
            if p['source_hash'] != body.source_hash or p['candidate_hash'] != body.candidate_hash:
                raise HTTPException(409, 'Candidate changed; review again')
            if s.info()['source_hash'] != body.source_hash or safety.is_frozen(conn):
                raise HTTPException(409, 'Workspace source or safety epoch changed')
            aid = secrets.token_hex(24)
            epoch = safety.get_epoch(conn)
            expires = int(time.time()) + 300
            conn.execute('INSERT INTO local_execution_approvals VALUES (?,?,?,?,?,?,?,?,?,?,0)',
                         (aid, wid, principal.user, principal.workspace, body.candidate_id,
                          body.source_hash, body.candidate_hash, body.preset, epoch, expires))
            conn.commit()
            return {'approval_id': aid, 'expires_in': 300, 'preset': body.preset}

    @app.post('/api/local-workspaces/{wid}/execution/run')
    def execution_run(wid: str, body: Apply, request: Request, principal=Depends(require_principal)):
        local(request)
        with lock:
            if not execution_capabilities()['configured']:
                raise HTTPException(409, 'Execution sandbox is disabled or invalidly configured')
            check_write()
            row = _execution_row(wid, body.approval_id, principal)
            now = int(time.time())
            if not row or row['consumed'] or row['expires_at'] < now or row['epoch'] != safety.get_epoch(conn):
                raise HTTPException(403, 'Execution approval missing, expired, consumed, or revoked')
            cur = conn.execute('UPDATE local_execution_approvals SET consumed=1 WHERE id=? AND consumed=0', (row['id'],))
            if cur.rowcount != 1:
                raise HTTPException(403, 'Execution approval already consumed')
            conn.commit()
            s = invoke(lambda: load(wid, principal))
            preview = invoke(lambda: s.preview(row['candidate_id']))
            if (s.info()['source_hash'] != row['source_hash'] or
                    preview['source_hash'] != row['source_hash'] or
                    preview['candidate_hash'] != row['candidate_hash']):
                raise HTTPException(409, 'Execution approval source or candidate is stale')
            epoch, candidate_id, candidate_hash, preset = row['epoch'], row['candidate_id'], row['candidate_hash'], row['preset']

        def frozen():
            return safety.is_frozen(conn) or safety.get_epoch(conn) != epoch
        try:
            evidence = sandbox_for(s).execute(candidate_id, preset, expected_candidate_sha=candidate_hash,
                                              check_frozen=frozen,
                                              cancelled=lambda: safety.get_epoch(conn) != epoch)
        except Exception:
            evidence = {'status': 'inconclusive', 'passed': False, 'reason': 'execution_error',
                        'candidate_id': candidate_id, 'candidate_sha': candidate_hash, 'preset': preset}
        evidence = _safe_evidence(evidence)
        with lock:
            conn.execute('INSERT INTO local_execution_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (secrets.token_hex(16), row['id'], wid, principal.user, principal.workspace,
                          candidate_id, row['source_hash'], candidate_hash, preset, epoch, now,
                          row['expires_at'], json.dumps(evidence, allow_nan=False)))
            conn.commit()
            events.emit(conn, 'local_execution_completed', actor=principal.user,
                        detail={'workspace_id': wid, 'candidate_id': candidate_id,
                                'preset': preset, 'status': evidence.get('status')})
        return evidence

    @app.get('/api/local-workspaces/{wid}/execution/evidence/{candidate_id}')
    def execution_evidence(wid: str, candidate_id: str, request: Request,
                           principal=Depends(require_principal)):
        local(request)
        with lock:
            load(wid, principal)
            rows = conn.execute('SELECT * FROM local_execution_evidence WHERE project=? AND owner=? AND workspace=? AND candidate_id=? ORDER BY created_at DESC',
                                (wid, principal.user, principal.workspace, candidate_id)).fetchall()
            return {'evidence': [{**dict(r), 'evidence': json.loads(r['evidence'])} for r in rows]}

    @app.post('/api/local-workspaces/{wid}/approvals')
    def approve(wid: str, body: Approval, request: Request, principal=Depends(require_principal)):
        import time
        local(request)
        with lock:
            check_write()
            if not body.confirmed:
                raise HTTPException(422, '必须明确确认完整差异')
            s = invoke(lambda: load(wid, principal))
            p = invoke(lambda: s.preview(body.candidate_id))
            if p['source_hash'] != body.source_hash or p['candidate_hash'] != body.candidate_hash:
                raise HTTPException(409, '候选差异已经变化，必须重新预览')
            if s.info()['source_hash'] != body.source_hash:
                raise HTTPException(409, '源目录已经变化，必须重新预览')
            aid = secrets.token_hex(24)
            conn.execute('INSERT INTO local_write_approvals VALUES (?,?,?,?,?,?,?,?,?,0)',
                         (aid, wid, principal.user, principal.workspace, body.candidate_id,
                          body.source_hash, body.candidate_hash, safety.get_epoch(conn), int(time.time()) + 300))
            conn.commit()
            events.emit(conn, 'local_write_approved', actor=principal.user,
                        detail={'workspace_id': wid, 'approval_id': aid, 'candidate_id': body.candidate_id,
                                'source_hash': body.source_hash, 'candidate_hash': body.candidate_hash})
            return {'approval_id': aid, 'expires_in': 300}

    @app.post('/api/local-workspaces/{wid}/apply')
    def apply(wid: str, body: Apply, request: Request, principal=Depends(require_principal)):
        import time
        local(request)
        with lock:
            check_write()
            s = invoke(lambda: load(wid, principal))
            row = conn.execute('SELECT * FROM local_write_approvals WHERE id=? AND project=? AND owner=? AND workspace=?',
                               (body.approval_id, wid, principal.user, principal.workspace)).fetchone()
            if not row or row['consumed'] or row['expires_at'] < int(time.time()) or row['epoch'] != safety.get_epoch(conn):
                raise HTTPException(403, '审批不存在、已使用、过期或因冻结失效')
            cur = conn.execute('UPDATE local_write_approvals SET consumed=1 WHERE id=? AND consumed=0', (row['id'],))
            conn.commit()
            if cur.rowcount != 1:
                raise HTTPException(403, '审批已使用')
            events.emit(conn, 'local_apply_started', actor=principal.user,
                        detail={'workspace_id': wid, 'candidate_id': row['candidate_id']})
            try:
                result = invoke(lambda: s.apply(row['candidate_id'], row['source_hash'], row['candidate_hash'], check_write=check_write))
            except Exception:
                events.emit(conn, 'local_apply_failed', actor=principal.user,
                            detail={'workspace_id': wid, 'candidate_id': row['candidate_id'], 'approval_consumed': True})
                raise
            applied_hash = s.info()['source_hash']
            version = next((v for v in s.versions() if v['commit'] == result['commit']), None)
            if applied_hash != row['candidate_hash'] or not version or version['source_hash'] != applied_hash:
                raise HTTPException(409, 'Actual applied snapshot does not match approved candidate')
            conn.execute('INSERT INTO local_application_receipts VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)',
                         (secrets.token_hex(16), wid, principal.user, principal.workspace,
                          row['candidate_id'], row['source_hash'], row['candidate_hash'],
                          result['commit'], applied_hash, row['epoch']))
            conn.commit()
            events.emit(conn, 'local_apply_completed', actor=principal.user, detail={'workspace_id': wid, **result})
            return result

    @app.get('/api/local-workspaces/{wid}/versions')
    def versions(wid: str, request: Request, principal=Depends(require_principal)):
        local(request)
        with lock:
            return invoke(lambda: {'versions': load(wid, principal).versions()})

    @app.get('/api/local-workspaces/{wid}/version')
    def version(wid: str, commit: str, path: str, request: Request, principal=Depends(require_principal)):
        local(request)
        with lock:
            return invoke(lambda: {'commit': commit, 'path': path, 'content': load(wid, principal).read_version(commit, path)})

    @app.get('/api/local-workspaces/{wid}/diff')
    def diff(wid: str, from_commit: str, to_commit: str, request: Request, principal=Depends(require_principal)):
        local(request)
        with lock:
            return invoke(lambda: load(wid, principal).diff_versions(from_commit, to_commit))
