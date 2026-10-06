"""Durable, demand-driven engineering scheduler (no threads, shell or model client).

Callbacks are TRUSTED server adapters, never deserialized from request bodies.
``step_handler(phase, context)`` performs read-only analysis / isolated proposal;
``executor(context)`` runs approved tests in an isolated runner. Only
``verify_applied(context)`` may attest external git/application evidence. A model's
claimed test/apply success is deliberately ignored. Call ``recover()`` once at
exclusive service startup before accepting requests, not alongside live workers.
"""
from __future__ import annotations

import copy
import json
import secrets
import sqlite3
import threading
from collections.abc import Mapping


class EngineeringRunError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _fail(code, message):
    raise EngineeringRunError(code, message)


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        _fail('invalid_input', f'{name} must be a nonempty string')
    return value


def validate_plan(plan):
    """Validate a supplied DAG; never generate or implicitly authorize a plan."""
    if isinstance(plan, dict):
        if set(plan) != {'nodes'}:
            _fail('invalid_plan', 'Plan must contain only nodes')
        plan = plan['nodes']
    if not isinstance(plan, list) or not 1 <= len(plan) <= 200:
        _fail('invalid_plan', 'Plan requires 1..200 nodes')
    nodes = []
    for item in plan:
        if not isinstance(item, dict) or set(item) - {'id', 'goal', 'depends_on', 'tests'}:
            _fail('invalid_plan', 'Unsupported node fields')
        nid = _text(item.get('id'), 'node id')
        goal = _text(item.get('goal'), 'node goal')
        deps = item.get('depends_on', [])
        tests = item.get('tests', ['test', 'lint'])
        if (not isinstance(deps, list) or any(not isinstance(d, str) for d in deps)
                or len(set(deps)) != len(deps)):
            _fail('invalid_plan', 'Dependencies must be unique ids')
        if not isinstance(tests, list) or not tests or any(not isinstance(t, str) or not t.strip() for t in tests):
            _fail('invalid_plan', 'Each node requires nonempty test/lint checks')
        if len(set(tests)) != len(tests):
            _fail('invalid_plan', 'Duplicate checks')
        nodes.append({'id': nid, 'goal': goal, 'depends_on': deps[:], 'tests': tests[:],
                      'status': 'pending', 'phase': 'analyze', 'attempt_token': None,
                      'candidate': None, 'evidence': None, 'repair_count': 0})
    ids = {n['id'] for n in nodes}
    if len(ids) != len(nodes) or any(d not in ids or d == n['id'] for n in nodes for d in n['depends_on']):
        _fail('invalid_plan', 'Duplicate ids, unknown dependencies or self dependency')
    done = set()
    while len(done) < len(nodes):
        ready = {n['id'] for n in nodes if n['id'] not in done and set(n['depends_on']) <= done}
        if not ready:
            _fail('invalid_plan', 'Cyclic dependencies')
        done |= ready
    return nodes


class EngineeringRunService:
    """All mutating calls require the original owner, epoch and source binding.

    Supply workspace_id on every API-facing call (it is mandatory). Epoch is a
    nonnegative integer. ``get`` is scoped identically to mutations. SQLite state
    is committed before callbacks start; late callback output loses its CAS.
    A caller-owned SQLite connection must permit the caller's threading model.
    """
    def __init__(self, database):
        self.conn = database if isinstance(database, sqlite3.Connection) else sqlite3.connect(
            str(database), check_same_thread=False, timeout=30)
        self._owns_connection = not isinstance(database, sqlite3.Connection)
        self._lock = threading.RLock()
        with self._lock:
            self.conn.execute('CREATE TABLE IF NOT EXISTS engineering_runs ('
                              'id TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL)')
            self.conn.commit()

    def close(self):
        if self._owns_connection:
            self.conn.close()

    def _load(self, run_id):
        row = self.conn.execute('SELECT revision,state FROM engineering_runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            _fail('not_found', 'Unknown engineering run')
        return row[0], json.loads(row[1])

    def _save(self, revision, state):
        # Optimistic CAS also protects independent service instances/connections.
        cursor = self.conn.execute('UPDATE engineering_runs SET revision=revision+1,state=? WHERE id=? AND revision=?',
                                   (json.dumps(state, allow_nan=False), state['id'], revision))
        if cursor.rowcount != 1:
            _fail('stale_attempt', 'Run changed concurrently')

    def _transaction(self, operation):
        with self._lock:
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                result = operation()
                self.conn.commit()
                return copy.deepcopy(result)
            except BaseException:
                self.conn.rollback()
                raise

    @staticmethod
    def _scope(state, *, workspace_id, owner, epoch, source_hash):
        expected = (state['workspace_id'], state['owner'], state['epoch'], state['source_hash'])
        if (workspace_id, owner, epoch, source_hash) != expected or type(epoch) is not int:
            _fail('binding_mismatch', 'Owner/workspace/epoch/source binding mismatch')

    def create(self, goal, plan, *, workspace_id, owner, epoch, source_hash,
               maxsteps=100, modelcalls=60, repairrounds=2):
        for name, value in [('goal', goal), ('workspace_id', workspace_id), ('owner', owner), ('source_hash', source_hash)]:
            _text(value, name)
        if type(epoch) is not int or epoch < 0:
            _fail('invalid_input', 'epoch must be a nonnegative integer')
        if any(type(v) is not int or v < minimum for v, minimum in
               [(maxsteps, 1), (modelcalls, 1), (repairrounds, 0)]):
            _fail('invalid_budget', 'Invalid budgets')
        state = dict(id=secrets.token_hex(16), goal=goal, workspace_id=workspace_id,
                     owner=owner, epoch=epoch, source_hash=source_hash, confirmed=False,
                     status='waiting_user', nodes=validate_plan(plan),
                     budgets={'maxsteps': maxsteps, 'modelcalls': modelcalls, 'repairrounds': repairrounds},
                     used={'steps': 0, 'modelcalls': 0}, reason='plan_confirmation')
        def insert():
            self.conn.execute('INSERT INTO engineering_runs VALUES (?,0,?)', (state['id'], json.dumps(state)))
            return state
        return self._transaction(insert)

    def get(self, run_id, **binding):
        with self._lock:
            _, state = self._load(run_id)
            self._scope(state, **binding)
            return copy.deepcopy(state)

    def _change(self, run_id, binding, change):
        def op():
            revision, state = self._load(run_id)
            self._scope(state, **binding)
            change(state)
            self._save(revision, state)
            return state
        return self._transaction(op)

    def confirm(self, run_id, **binding):
        def change(s):
            if s['confirmed'] or s['status'] != 'waiting_user':
                _fail('invalid_state', 'Only a new plan may be confirmed')
            s.update(confirmed=True, status='pending', reason=None)
        return self._change(run_id, binding, change)

    @staticmethod
    def _invalidate(s):
        for n in s['nodes']:
            if n['status'] == 'in_progress':
                n.update(status='in_doubt', reason='interrupted_callback')
            n['attempt_token'] = None

    def pause(self, run_id, **binding):
        def change(s):
            if s['status'] in ('stopped', 'completed'):
                _fail('invalid_state', 'Terminal run')
            self._invalidate(s)
            s.update(status='paused', reason='user_pause')
        return self._change(run_id, binding, change)

    def stop(self, run_id, **binding):
        def change(s):
            if s['status'] == 'completed':
                _fail('invalid_state', 'Completed run')
            self._invalidate(s)
            s.update(status='stopped', reason='user_stop')
        return self._change(run_id, binding, change)

    def resume(self, run_id, **binding):
        def change(s):
            if s['status'] != 'paused':
                _fail('invalid_state', 'Only paused runs may resume')
            s['status'] = 'blocked' if any(n['status'] == 'in_doubt' for n in s['nodes']) else (
                'pending' if s['confirmed'] else 'waiting_user')
            s['reason'] = 'in_doubt' if s['status'] == 'blocked' else None
        return self._change(run_id, binding, change)

    def recover(self):
        """Exclusive startup recovery: never replay a potentially executed stage."""
        def op():
            recovered = []
            for rid, in self.conn.execute('SELECT id FROM engineering_runs').fetchall():
                revision, s = self._load(rid)
                if any(n['status'] == 'in_progress' for n in s['nodes']):
                    self._invalidate(s)
                    if s['status'] not in ('paused', 'stopped'):
                        s.update(status='blocked', reason='in_doubt')
                    self._save(revision, s)
                    recovered.append(rid)
            return recovered
        return self._transaction(op)

    def retry(self, run_id, node_id, *, confirmed, **binding):
        """Explicit bounded repair of a failed node; never replay in-doubt work."""
        def change(s):
            if confirmed is not True or s['status'] in ('paused', 'stopped', 'completed'):
                _fail('invalid_state', 'Repair needs explicit confirmation and active run')
            n = self._node(s, node_id)
            if n['status'] != 'failed':
                _fail('invalid_state', 'Only failed nodes can be repaired')
            if n['repair_count'] >= s['budgets']['repairrounds']:
                _fail('budget_exhausted', 'Repair budget exhausted')
            n.update(status='pending', phase='analyze', candidate=None, evidence=None,
                     repair_count=n['repair_count'] + 1, reason=None)
            s.update(status='pending', reason=None)
        return self._change(run_id, binding, change)

    @staticmethod
    def _node(s, node_id):
        for n in s['nodes']:
            if n['id'] == node_id:
                return n
        _fail('not_found', 'Unknown node')

    def step(self, run_id, *, step_handler=None, executor=None, execution_approved=False,
             safety_guard=None, **binding):
        """Execute at most one stage. Handler receives (phase, context).

        ``safety_guard`` is a trusted zero-argument callback evaluated inside the
        final SQLite transaction immediately before the claimed outcome is
        committed. A false result or exception quarantines the attempt as
        ``in_doubt``/``blocked`` and discards the callback result.

        Analysis outcome: {status: 'completed', evidence: ...}.
        Proposal outcome: {status: 'completed', candidate: {candidate_hash,
        source_hash, isolated: True, ...}}. Candidate becomes waiting_user only
        after all required executor checks have passed with nonempty evidence.
        Executor outcome: {checks: [{name, passed: True, evidence: ...}, ...]}.
        Missing/unapproved executor is blocked, never passed. Context is a copy.
        """
        claim = {}
        def reserve(s):
            if not s['confirmed'] or s['status'] in ('paused', 'stopped', 'completed'):
                _fail('invalid_state', 'Run is unconfirmed, paused or terminal')
            if any(n['status'] in ('in_progress', 'in_doubt') for n in s['nodes']):
                _fail('invalid_state', 'Unresolved attempt; no concurrent or blind replay')
            done = {n['id'] for n in s['nodes'] if n['status'] == 'completed'}
            n = next((n for n in s['nodes'] if n['status'] in ('pending', 'blocked') and set(n['depends_on']) <= done), None)
            if n is None:
                return
            phase = n['phase']
            if s['used']['steps'] >= s['budgets']['maxsteps'] or (
                phase != 'verify' and s['used']['modelcalls'] >= s['budgets']['modelcalls']):
                n.update(status='blocked', reason='budget_exhausted')
                s.update(status='blocked', reason='budget_exhausted')
                return
            if phase == 'verify' and (not callable(executor) or execution_approved is not True):
                n.update(status='blocked', reason='executor_unavailable_or_unapproved')
                s.update(status='blocked', reason=n['reason'])
                return
            if phase != 'verify' and not callable(step_handler):
                n.update(status='blocked', reason='model_adapter_unavailable')
                s.update(status='blocked', reason=n['reason'])
                return
            token = secrets.token_hex(24)
            n.update(status='in_progress', attempt_token=token, reason=None)
            s.update(status='in_progress', reason=None)
            s['used']['steps'] += 1
            s['used']['modelcalls'] += int(phase != 'verify')
            claim.update(node_id=n['id'], token=token, phase=phase,
                         context={'run': copy.deepcopy(s), 'node': copy.deepcopy(n)})
        state = self._change(run_id, binding, reserve)
        if not claim:
            return state
        try:
            outcome = executor(claim['context']) if claim['phase'] == 'verify' else step_handler(claim['phase'], claim['context'])
            # Serialization errors are failed outcomes, not stranded active claims.
            json.dumps(outcome, allow_nan=False)
        except Exception:
            outcome = {'status': 'failed', 'reason': 'adapter_error'}
        def finish(s):
            n = self._node(s, claim['node_id'])
            if n['attempt_token'] != claim['token'] or n['status'] != 'in_progress' or s['status'] != 'in_progress':
                _fail('stale_attempt', 'Attempt revoked; late output discarded')
            if safety_guard is not None:
                try:
                    guard_result = safety_guard()
                    guard_ok = guard_result is None or guard_result is True
                except Exception:
                    guard_ok = False
                if not guard_ok:
                    n.update(status='in_doubt', attempt_token=None, reason='safety_guard_revoked')
                    s.update(status='blocked', reason='safety_guard_revoked')
                    return
            n['attempt_token'] = None
            phase = claim['phase']
            valid = isinstance(outcome, Mapping)
            if phase == 'verify':
                checks = outcome.get('checks', []) if valid else []
                valid = isinstance(checks, list) and len(checks) == len(n['tests']) and all(
                    isinstance(c, dict) and isinstance(c.get('name'), str)
                    and c.get('passed') is True and bool(c.get('evidence')) for c in checks)
                valid = valid and sorted(c.get('name', '') for c in checks) == sorted(n['tests'])
                if valid:
                    n.update(status='waiting_user', evidence=outcome, phase='apply')
            elif valid and outcome.get('status') == 'completed':
                if phase == 'analyze':
                    valid = bool(outcome.get('evidence'))
                    if valid:
                        n.update(status='pending', phase='propose', analysis=outcome['evidence'])
                else:
                    candidate = outcome.get('candidate')
                    valid = (isinstance(candidate, dict) and candidate.get('isolated') is True
                             and isinstance(candidate.get('candidate_hash'), str) and bool(candidate['candidate_hash'])
                             and candidate.get('source_hash') == s['source_hash'])
                    if valid:
                        n.update(status='pending', phase='verify', candidate=candidate)
            else:
                valid = False
            if not valid:
                n.update(status='failed', reason='invalid_or_failed_outcome')
            s.update(status='waiting_user' if n['status'] == 'waiting_user' else (
                'failed' if n['status'] == 'failed' else 'pending'), reason=n.get('reason'))
        return self._change(run_id, binding, finish)

    def record_applied(self, run_id, node_id, candidate_hash, source_hash, *, confirmed,
                       verify_applied=None, safety_guard=None, **binding):
        """Record external apply ONLY after a trusted git-evidence callback passes.

        Callback(context) must return {verified: True, candidate_hash: ..., 
        source_hash: <original>, git_evidence: <nonempty>, applied_source_hash:
        <new baseline>}. No original write is performed here. Verification gets
        its own revocable attempt; run source binding advances atomically, so old
        confirmations cannot authorize subsequent nodes against stale source.
        """
        if confirmed is not True or not callable(verify_applied):
            _fail('approval_required', 'Explicit confirmation and trusted verifier required')
        binding = dict(binding, source_hash=source_hash)
        claim = {}
        def reserve(s):
            if s['status'] in ('paused', 'stopped', 'completed') or any(n['status'] in ('in_progress', 'in_doubt') for n in s['nodes']):
                _fail('invalid_state', 'Run cannot accept application evidence')
            n = self._node(s, node_id)
            if (n['status'] != 'waiting_user' or n['phase'] != 'apply' or source_hash != s['source_hash']
                    or candidate_hash != n['candidate']['candidate_hash']):
                _fail('binding_mismatch', 'Candidate/source/status mismatch')
            if s['used']['steps'] >= s['budgets']['maxsteps']:
                _fail('budget_exhausted', 'Application verification budget exhausted')
            s['used']['steps'] += 1
            token = secrets.token_hex(24)
            n.update(status='in_progress', attempt_token=token)
            s['status'] = 'in_progress'
            claim.update(token=token, context={'run': copy.deepcopy(s), 'node': copy.deepcopy(n)})
        self._change(run_id, binding, reserve)
        try:
            evidence = verify_applied(claim['context'])
            json.dumps(evidence, allow_nan=False)
        except Exception:
            evidence = None
        def finish(s):
            n = self._node(s, node_id)
            if n['attempt_token'] != claim['token'] or n['status'] != 'in_progress' or s['status'] != 'in_progress':
                _fail('stale_attempt', 'Application verification revoked')
            if safety_guard is not None:
                try:
                    guard_result = safety_guard()
                    guard_ok = guard_result is None or guard_result is True
                except Exception:
                    guard_ok = False
                if not guard_ok:
                    n.update(status='in_doubt', attempt_token=None, reason='safety_guard_revoked')
                    s.update(status='blocked', reason='safety_guard_revoked')
                    return
            n['attempt_token'] = None
            valid = (isinstance(evidence, dict) and evidence.get('verified') is True
                     and evidence.get('candidate_hash') == candidate_hash and evidence.get('source_hash') == source_hash
                     and bool(evidence.get('git_evidence')) and isinstance(evidence.get('applied_source_hash'), str)
                     and bool(evidence['applied_source_hash']))
            if not valid:
                n.update(status='waiting_user', reason='application_not_verified')
                s.update(status='waiting_user', reason=n['reason'])
                return
            n.update(status='completed', phase='done', application_evidence=evidence, reason=None)
            s['source_hash'] = evidence['applied_source_hash']
            # Any candidate prepared against the old baseline requires re-analysis.
            for other in s['nodes']:
                if other['id'] != node_id and other['status'] not in ('completed', 'in_doubt', 'failed'):
                    other.update(status='pending', phase='analyze', candidate=None, evidence=None)
            s.update(status='completed' if all(n['status'] == 'completed' for n in s['nodes']) else 'pending', reason=None)
        return self._change(run_id, binding, finish)

    def approve_result(self, run_id, node_id, candidate_hash, source_hash, *, confirmed,
                       verify_applied=None, **binding):
        """Alias for record_applied; a caller's success assertion is insufficient."""
        return self.record_applied(run_id, node_id, candidate_hash, source_hash,
                                   confirmed=confirmed, verify_applied=verify_applied, **binding)
