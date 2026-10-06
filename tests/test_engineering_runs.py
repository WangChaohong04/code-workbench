"""Fail-closed durable engineering scheduler contract tests."""
import json
import threading

import pytest

from longflow.engineering_runs import EngineeringRunError, EngineeringRunService


BIND = dict(workspace_id='ws', owner='alice', epoch=7, source_hash='source-1')
PLAN = [{'id': 'a', 'goal': 'Fix bug'}, {'id': 'b', 'goal': 'Follow up', 'depends_on': ['a']}]


@pytest.fixture
def service(tmp_path):
    s = EngineeringRunService(tmp_path / 'runs.sqlite')
    yield s
    s.close()


def create(s, **kwargs):
    return s.create('Deliver change', PLAN, **BIND, **kwargs)['id']


def handler(phase, context):
    if phase == 'analyze':
        return {'status': 'completed', 'evidence': 'read-only analysis'}
    return {'status': 'completed', 'candidate': {
        'candidate_hash': 'candidate-1', 'source_hash': context['run']['source_hash'],
        'isolated': True, 'path': 'server-owned candidate'},
        'tests_passed': True, 'applied': True}


def executor(context):
    return {'checks': [{'name': name, 'passed': True, 'evidence': 'runner log'}
                       for name in context['node']['tests']]}


def prepared(s, rid):
    s.confirm(rid, **BIND)
    s.step(rid, step_handler=handler, **BIND)
    return s.step(rid, step_handler=handler, **BIND)


def waiting(s, rid):
    prepared(s, rid)
    return s.step(rid, executor=executor, execution_approved=True, **BIND)


def verifier(context):
    return {'verified': True, 'candidate_hash': context['node']['candidate']['candidate_hash'],
            'source_hash': context['run']['source_hash'], 'git_evidence': {'commit': 'abc', 'diff_checked': True},
            'applied_source_hash': 'source-2'}


@pytest.mark.parametrize('evidence', [None, '', {}, []])
def test_analysis_cannot_advance_without_evidence(service, evidence):
    rid = create(service)
    service.confirm(rid, **BIND)
    state = service.step(rid, step_handler=lambda phase, context:
        {'status': 'completed', 'evidence': evidence}, **BIND)
    assert state['status'] == 'failed'
    assert state['nodes'][0]['phase'] == 'analyze'
    assert state['nodes'][0]['reason'] == 'invalid_or_failed_outcome'
    assert state['nodes'][0]['candidate'] is None


def test_explicit_confirmation_and_one_phase_per_step(service):
    rid = create(service)
    with pytest.raises(EngineeringRunError, match='unconfirmed'):
        service.step(rid, step_handler=handler, **BIND)
    s = prepared(service, rid)
    assert s['nodes'][0]['phase'] == 'verify'
    assert s['nodes'][0]['status'] == 'pending'
    assert s['nodes'][1]['phase'] == 'analyze'
    assert s['used'] == {'steps': 2, 'modelcalls': 2}
    s = service.step(rid, executor=executor, execution_approved=True, **BIND)
    assert s['status'] == 'waiting_user'
    assert s['nodes'][0]['status'] == 'waiting_user'
    # No automatic original apply; dependency never advances before evidence.
    assert service.step(rid, step_handler=handler, **BIND)['nodes'][1]['status'] == 'pending'


@pytest.mark.parametrize('field,value', [('workspace_id', 'other'), ('owner', 'mallory'),
                                        ('epoch', 8), ('source_hash', 'changed')])
def test_all_operations_scoped(service, field, value):
    rid = create(service)
    bad = dict(BIND, **{field: value})
    for method in (service.get, service.confirm, service.pause, service.stop):
        with pytest.raises(EngineeringRunError) as exc:
            method(rid, **bad)
        assert exc.value.code == 'binding_mismatch'


@pytest.mark.parametrize('plan', [[], [{'id': 'a', 'goal': 'x', 'depends_on': ['missing']}],
                                  [{'id': 'a', 'goal': 'x', 'depends_on': ['b']},
                                   {'id': 'b', 'goal': 'y', 'depends_on': ['a']}],
                                  [{'id': 'a', 'goal': 'x', 'apply': True}],
                                  [{'id': 'a', 'goal': 'x', 'tests': []}]])
def test_plan_validation(service, plan):
    with pytest.raises(EngineeringRunError):
        service.create('Goal', plan, **BIND)


@pytest.mark.parametrize('invalid_executor', [None, True, 'runner', {}, []])
@pytest.mark.parametrize('approved', [False, True])
def test_absent_executor_never_passes(service, approved, invalid_executor):
    rid = create(service)
    prepared(service, rid)
    s = service.step(rid, executor=invalid_executor, execution_approved=approved, **BIND)
    assert s['nodes'][0]['status'] == 'blocked'
    assert s['nodes'][0]['evidence'] is None
    assert s['used']['steps'] == 2
    assert service.step(rid, executor=executor, execution_approved=True, **BIND)['status'] == 'waiting_user'


@pytest.mark.parametrize('invalid_handler', [None, True, 'model', {}, []])
def test_invalid_model_handler_blocks_without_spending_budget(service, invalid_handler):
    rid = create(service)
    service.confirm(rid, **BIND)
    state = service.step(rid, step_handler=invalid_handler, **BIND)
    assert state['status'] == 'blocked'
    assert state['nodes'][0]['reason'] == 'model_adapter_unavailable'
    assert state['nodes'][0]['attempt_token'] is None
    assert state['used'] == {'steps': 0, 'modelcalls': 0}
    assert service.step(rid, step_handler=handler, **BIND)['nodes'][0]['phase'] == 'propose'


def test_unapproved_executor_not_called(service):
    rid = create(service)
    prepared(service, rid)
    def forbidden(_):
        pytest.fail('unapproved executor called')
    assert service.step(rid, executor=forbidden, **BIND)['status'] == 'blocked'


@pytest.mark.parametrize('outcome', [{'status': 'completed', 'tests_passed': True},
                                    {'checks': [{'name': 'test', 'passed': True, 'evidence': 'log'}]},
                                    {'checks': [{'name': 'test', 'passed': True, 'evidence': ''},
                                                {'name': 'lint', 'passed': True, 'evidence': 'log'}]}])
def test_test_success_must_cover_all_checks_with_evidence(service, outcome):
    rid = create(service)
    prepared(service, rid)
    s = service.step(rid, executor=lambda _: outcome, execution_approved=True, **BIND)
    assert s['nodes'][0]['status'] == 'failed'
    assert s['nodes'][1]['status'] == 'pending'


@pytest.mark.parametrize('invalid_verifier', [None, True, 'verified', {}, []])
def test_invalid_application_verifier_does_not_consume_attempt_budget(service, invalid_verifier):
    rid = create(service)
    before = waiting(service, rid)
    with pytest.raises(EngineeringRunError) as error:
        service.record_applied(rid, 'a', 'candidate-1', 'source-1', confirmed=True,
            verify_applied=invalid_verifier, workspace_id='ws', owner='alice', epoch=7)
    assert error.value.code == 'approval_required'
    assert service.get(rid, **BIND) == before


def test_application_requires_trusted_callback_and_exact_hash(service):
    rid = create(service)
    waiting(service, rid)
    with pytest.raises(EngineeringRunError) as exc:
        service.approve_result(rid, 'a', 'candidate-1', 'source-1', confirmed=True,
                               workspace_id='ws', owner='alice', epoch=7)
    assert exc.value.code == 'approval_required'
    with pytest.raises(EngineeringRunError):
        service.record_applied(rid, 'a', 'wrong', 'source-1', confirmed=True,
                               verify_applied=verifier, workspace_id='ws', owner='alice', epoch=7)
    s = service.record_applied(rid, 'a', 'candidate-1', 'source-1', confirmed=True,
                               verify_applied=lambda _: {'verified': True}, workspace_id='ws', owner='alice', epoch=7)
    assert s['nodes'][0]['status'] == 'waiting_user'
    s = service.approve_result(rid, 'a', 'candidate-1', 'source-1', confirmed=True,
                              verify_applied=verifier, workspace_id='ws', owner='alice', epoch=7)
    assert s['nodes'][0]['status'] == 'completed'
    assert s['source_hash'] == 'source-2'
    with pytest.raises(EngineeringRunError):
        service.step(rid, step_handler=handler, **BIND)
    s = service.step(rid, step_handler=handler, **dict(BIND, source_hash='source-2'))
    assert s['nodes'][1]['phase'] == 'propose'


@pytest.mark.parametrize('control', ['pause', 'stop'])
def test_late_callback_cannot_write_after_control(service, control):
    rid = create(service)
    service.confirm(rid, **BIND)
    started, release = threading.Event(), threading.Event()
    failures = []
    def slow(phase, context):
        started.set()
        assert release.wait(5)
        return handler(phase, context)
    def work():
        try:
            service.step(rid, step_handler=slow, **BIND)
        except EngineeringRunError as exc:
            failures.append(exc.code)
    thread = threading.Thread(target=work)
    thread.start()
    assert started.wait(5)
    getattr(service, control)(rid, **BIND)
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert failures == ['stale_attempt']
    s = service.get(rid, **BIND)
    assert s['status'] == ('paused' if control == 'pause' else 'stopped')
    assert s['nodes'][0]['status'] == 'in_doubt'
    if control == 'pause':
        assert service.resume(rid, **BIND)['status'] == 'blocked'
    with pytest.raises(EngineeringRunError):
        service.step(rid, step_handler=handler, **BIND)


def test_persistence_and_explicit_startup_recovery(tmp_path):
    path = tmp_path / 'runs.sqlite'
    s = EngineeringRunService(path)
    rid = create(s)
    s.confirm(rid, **BIND)
    # Simulate process death with a durable claim, without actually killing pytest.
    revision, state = s._load(rid)
    state['status'] = 'in_progress'
    state['nodes'][0].update(status='in_progress', attempt_token='orphan')
    s._save(revision, state)
    s.conn.commit()
    s.close()
    s = EngineeringRunService(path)
    assert s.recover() == [rid]
    assert s.recover() == []
    state = s.get(rid, **BIND)
    assert state['nodes'][0]['status'] == 'in_doubt'
    assert state['nodes'][0]['attempt_token'] is None
    with pytest.raises(EngineeringRunError):
        s.step(rid, step_handler=handler, **BIND)
    s.close()


def test_budgets_and_repairs(service):
    rid = create(service, maxsteps=1)
    service.confirm(rid, **BIND)
    service.step(rid, step_handler=handler, **BIND)
    s = service.step(rid, step_handler=handler, **BIND)
    assert s['reason'] == 'budget_exhausted'
    assert s['used']['steps'] == 1
    rid = create(service, modelcalls=1)
    service.confirm(rid, **BIND)
    service.step(rid, step_handler=handler, **BIND)
    assert service.step(rid, step_handler=handler, **BIND)['reason'] == 'budget_exhausted'
    rid = create(service, repairrounds=1)
    service.confirm(rid, **BIND)
    service.step(rid, step_handler=lambda *_: {'status': 'failed'}, **BIND)
    service.retry(rid, 'a', confirmed=True, **BIND)
    service.step(rid, step_handler=lambda *_: {'status': 'failed'}, **BIND)
    with pytest.raises(EngineeringRunError) as exc:
        service.retry(rid, 'a', confirmed=True, **BIND)
    assert exc.value.code == 'budget_exhausted'


def test_nonisolated_proposal_fails(service):
    rid = create(service)
    service.confirm(rid, **BIND)
    service.step(rid, step_handler=handler, **BIND)
    def unsafe(phase, context):
        result = handler(phase, context)
        result['candidate']['isolated'] = False
        return result
    assert service.step(rid, step_handler=unsafe, **BIND)['nodes'][0]['status'] == 'failed'


def test_adapter_exception_checkpointed(service):
    rid = create(service)
    service.confirm(rid, **BIND)
    def broken(*_):
        raise RuntimeError('secret')
    s = service.step(rid, step_handler=broken, **BIND)
    assert s['nodes'][0]['status'] == 'failed'
    assert 'secret' not in json.dumps(s)


def test_entire_run_completes_only_after_two_external_attestations(service):
    rid = create(service)
    waiting(service, rid)
    service.record_applied(rid, 'a', 'candidate-1', 'source-1', confirmed=True,
                           verify_applied=verifier, workspace_id='ws', owner='alice', epoch=7)
    binding = dict(BIND, source_hash='source-2')
    service.step(rid, step_handler=handler, **binding)
    service.step(rid, step_handler=handler, **binding)
    assert service.step(rid, executor=executor, execution_approved=True, **binding)['status'] == 'waiting_user'
    s = service.record_applied(rid, 'b', 'candidate-1', 'source-2', confirmed=True,
                               verify_applied=verifier, workspace_id='ws', owner='alice', epoch=7)
    assert s['status'] == 'completed'
    assert all(n['status'] == 'completed' for n in s['nodes'])
    with pytest.raises(EngineeringRunError):
        service.step(rid, step_handler=handler, **binding)


def test_application_verification_has_budget(service):
    rid = create(service, maxsteps=3)
    waiting(service, rid)
    with pytest.raises(EngineeringRunError) as exc:
        service.record_applied(rid, 'a', 'candidate-1', 'source-1', confirmed=True,
                               verify_applied=verifier, workspace_id='ws', owner='alice', epoch=7)
    assert exc.value.code == 'budget_exhausted'
    assert service.get(rid, **BIND)['nodes'][0]['status'] == 'waiting_user'


@pytest.mark.parametrize('guard_value', [None, True])
def test_valid_safety_guard_allows_step_and_application_commit(service, guard_value):
    rid = create(service)
    service.confirm(rid, **BIND)
    state = service.step(rid, step_handler=handler, safety_guard=lambda: guard_value, **BIND)
    assert state['nodes'][0]['phase'] == 'propose'
    assert state['nodes'][0]['status'] == 'pending'
    service.step(rid, step_handler=handler, **BIND)
    service.step(rid, executor=executor, execution_approved=True, **BIND)
    state = service.record_applied(rid, 'a', 'candidate-1', 'source-1', confirmed=True,
        verify_applied=verifier, safety_guard=lambda: guard_value,
        workspace_id='ws', owner='alice', epoch=7)
    assert state['nodes'][0]['status'] == 'completed'
    assert state['source_hash'] == 'source-2'


@pytest.mark.parametrize('guard_value', [False, 0, 1, 'true', [], {}])
def test_step_safety_guard_revokes_result_before_finish(service, guard_value):
    rid = create(service)
    service.confirm(rid, **BIND)
    s = service.step(rid, step_handler=handler, safety_guard=lambda: guard_value, **BIND)
    assert s['status'] == 'blocked'
    assert s['nodes'][0]['status'] == 'in_doubt'
    assert s['nodes'][0]['candidate'] is None
    assert s['nodes'][0]['attempt_token'] is None


@pytest.mark.parametrize('guard_value', [False, 0, 1, 'true', [], {}])
def test_application_safety_guard_quarantines_after_external_callback(service, guard_value):
    rid = create(service)
    waiting(service, rid)
    calls = []
    def verifier_then_guard(ctx):
        calls.append(ctx['node']['candidate']['candidate_hash'])
        return verifier(ctx)
    s = service.record_applied(rid, 'a', 'candidate-1', 'source-1', confirmed=True,
                               verify_applied=verifier_then_guard, safety_guard=lambda: guard_value,
                               workspace_id='ws', owner='alice', epoch=7)
    assert calls == ['candidate-1']
    assert s['status'] == 'blocked'
    assert s['nodes'][0]['status'] == 'in_doubt'
    assert s['nodes'][0]['attempt_token'] is None
    assert s['source_hash'] == 'source-1'


@pytest.mark.parametrize('stage', ['step', 'application'])
def test_safety_guard_exception_quarantines_callback_result(service, stage):
    rid = create(service)
    def unavailable():
        raise RuntimeError('private safety backend error')
    if stage == 'step':
        service.confirm(rid, **BIND)
        state = service.step(rid, step_handler=handler, safety_guard=unavailable, **BIND)
    else:
        waiting(service, rid)
        state = service.record_applied(rid, 'a', 'candidate-1', 'source-1', confirmed=True,
            verify_applied=verifier, safety_guard=unavailable,
            workspace_id='ws', owner='alice', epoch=7)
    assert state['status'] == 'blocked'
    assert state['nodes'][0]['status'] == 'in_doubt'
    assert state['nodes'][0]['attempt_token'] is None
    assert state['source_hash'] == 'source-1'
    assert 'private safety backend error' not in json.dumps(state)


def test_idle_pause_resume_and_terminal_stop(service):
    rid = create(service)
    service.confirm(rid, **BIND)
    service.pause(rid, **BIND)
    assert service.resume(rid, **BIND)['status'] == 'pending'
    service.stop(rid, **BIND)
    with pytest.raises(EngineeringRunError):
        service.resume(rid, **BIND)
