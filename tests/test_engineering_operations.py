"""Real local Git/recovery tests; push transport is mocked, no production I/O."""
import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

from longflow.code_workspace import CodeWorkspaceError
from longflow.engineering_operations import EngineeringOperations, TrustedRepository
from longflow.local_workspace import LocalWorkspace


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test',
                             '-c', 'user.email=test@localhost', *args],
                            capture_output=True, check=True, shell=False)
    return result.stdout.decode().strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'native'
    root.mkdir()
    git(root, 'init', '--template=', '-b', 'main')
    (root / 'a.txt').write_bytes(b'first\n')
    git(root, 'add', 'a.txt')
    git(root, 'commit', '-m', 'first')
    git(root, 'branch', 'target')
    (root / 'a.txt').write_bytes(b'second\n')
    git(root, 'commit', '-am', 'second')
    return root.resolve()


def ops(repo, **kwargs):
    return EngineeringOperations(repository=TrustedRepository(str(repo)),
                                 check_write=lambda: None, **kwargs)


def error(code, fn):
    with pytest.raises(CodeWorkspaceError) as exc:
        fn()
    assert exc.value.code == code


def test_not_connected_production_and_no_implicit_repository():
    service = EngineeringOperations()
    assert service.capabilities()['git_branch']['status'] == 'not_connected'
    assert service.production_database()['status'] == 'blocked'
    assert service.production_deployment()['status'] == 'blocked'
    error('not_connected', lambda: service.propose_branch('feature', 'main'))


@pytest.mark.parametrize('field', ['epoch', 'clock', 'check_write', 'consume_approval', 'confirm_push'])
@pytest.mark.parametrize('value', [True, 0, 'adapter', {}, []])
def test_invalid_control_adapter_rejected_before_repository_access(field, value, monkeypatch):
    def forbidden(*args):
        pytest.fail('Invalid callback configuration must not access Git')
    monkeypatch.setattr(EngineeringOperations, '_git', forbidden)
    error('invalid_config', lambda: EngineeringOperations(**{field: value}))


@pytest.mark.parametrize('value', [True, 1.0, None, -1, '1'])
def test_invalid_epoch_cannot_register_operation_proposal(value):
    service = EngineeringOperations(epoch=lambda: value)
    error('control_unavailable', lambda: service._proposal('local_recovery', {}))
    assert service._pending == {}


@pytest.mark.parametrize('value', [True, 1.0, None, -1, '1'])
def test_invalid_execution_epoch_consumes_proposal_before_any_callbacks(value):
    current = [1]
    callbacks = []
    service = EngineeringOperations(epoch=lambda: current[0],
        check_write=lambda: callbacks.append('write'),
        consume_approval=lambda proposal: callbacks.append('approval') or True)
    proposal = service._proposal('local_recovery', {})
    current[0] = value
    error('control_unavailable', lambda: service.execute(proposal))
    assert callbacks == []
    current[0] = 1
    error('approval_consumed', lambda: service.execute(proposal))
    assert callbacks == []


@pytest.mark.parametrize('value', [float('nan'), float('inf'), None, True, '10'])
def test_invalid_clock_rejected_at_registration_and_execution(value):
    current = [value]
    callbacks = []
    service = EngineeringOperations(clock=lambda: current[0], check_write=lambda: callbacks.append('write'))
    error('control_unavailable', lambda: service._proposal('local_recovery', {}))
    assert service._pending == {}
    current[0] = 10
    proposal = service._proposal('local_recovery', {})
    current[0] = value
    error('control_unavailable', lambda: service.execute(proposal))
    assert callbacks == []
    current[0] = 10
    error('approval_consumed', lambda: service.execute(proposal))


@pytest.mark.parametrize('value', [0, 1, 'false', [], {}])
def test_invalid_write_guard_result_blocks_before_approval_and_consumes_proposal(value):
    calls = []
    service = EngineeringOperations(check_write=lambda: value,
        consume_approval=lambda proposal: calls.append(proposal) or True)
    proposal = service._proposal('local_recovery', {})
    error('control_unavailable', lambda: service.execute(proposal))
    assert calls == []
    service.check_write = lambda: False
    error('approval_consumed', lambda: service.execute(proposal))


@pytest.mark.parametrize('value', [None, False])
def test_write_guard_supported_success_returns(value):
    EngineeringOperations(check_write=lambda: value)._allow()


@pytest.mark.parametrize('change', ['epoch', 'invalid_epoch', 'freeze'])
def test_safety_change_during_ref_update_is_in_doubt(repo, monkeypatch, change):
    epoch, frozen = [1], [False]
    service = EngineeringOperations(repository=TrustedRepository(str(repo)),
        epoch=lambda: epoch[0], check_write=lambda: frozen[0], consume_approval=lambda proposal: True)
    proposal = service.propose_branch('feature', 'main')
    original_git = service._git
    def mutate_then_revoke(*args):
        result = original_git(*args)
        if args[0] == 'update-ref':
            if change == 'freeze':
                frozen[0] = True
            else:
                epoch[0] = 2 if change == 'epoch' else None
        return result
    monkeypatch.setattr(service, '_git', mutate_then_revoke)
    error('in_doubt', lambda: service.execute(proposal))
    assert git(repo, 'rev-parse', 'feature') == git(repo, 'rev-parse', 'main')
    epoch[0], frozen[0] = 1, False
    error('approval_consumed', lambda: service.execute(proposal))


def test_ref_updated_but_result_lost_is_in_doubt_and_not_replayed(repo, monkeypatch):
    service = ops(repo, consume_approval=lambda proposal: True)
    proposal = service.propose_branch('feature', 'main')
    original = service._git
    writes = []
    def lose_result(*args):
        result = original(*args)
        if args[0] == 'update-ref':
            writes.append(args)
            raise CodeWorkspaceError('git_failed', 'mock result lost after ref update')
        return result
    monkeypatch.setattr(service, '_git', lose_result)
    error('in_doubt', lambda: service.execute(proposal))
    assert git(repo, 'rev-parse', 'feature') == git(repo, 'rev-parse', 'main')
    error('approval_consumed', lambda: service.execute(proposal))
    assert len(writes) == 1


def test_real_branch_approval_digest_and_single_use(repo):
    seen = []
    service = ops(repo, consume_approval=lambda p: seen.append(p.to_dict()) or True)
    p = service.propose_branch('feature', 'main')
    assert 'refs/heads/feature' not in git(repo, 'for-each-ref')
    assert len(p.digest) == 64 and p.payload['diff'] == ''
    data = p.payload
    data['ref'] = 'refs/heads/evil'
    assert p.payload['ref'] == 'refs/heads/feature'
    error('invalid_proposal', lambda: service.execute(dataclasses.replace(p, epoch=99)))
    service.execute(p)
    assert git(repo, 'rev-parse', 'feature') == git(repo, 'rev-parse', 'main')
    assert seen[0]['digest'] == p.digest
    error('approval_consumed', lambda: service.execute(p))


def test_default_closed_permission_and_approval(repo):
    s = EngineeringOperations(repository=TrustedRepository(str(repo)))
    p = s.propose_branch('new', 'main')
    error('permission_denied', lambda: s.execute(p))
    s = ops(repo)
    error('approval_required', lambda: s.execute(s.propose_branch('new', 'main')))
    assert 'refs/heads/new' not in git(repo, 'for-each-ref')


def test_expiry_epoch_stale_callback_and_freeze(repo):
    now, epoch = [10], [2]
    s = ops(repo, consume_approval=lambda _: True, clock=lambda: now[0], epoch=lambda: epoch[0])
    p = s.propose_branch('new', 'main')
    now[0] = p.expires_at
    error('approval_expired', lambda: s.execute(p))
    now[0] = 10
    error('approval_consumed', lambda: s.execute(p))
    p = s.propose_branch('new', 'main')
    epoch[0] = 3
    error('approval_expired', lambda: s.execute(p))
    epoch[0] = 2
    error('approval_consumed', lambda: s.execute(p))
    s = ops(repo, consume_approval=lambda _: True)
    p = s.propose_branch('new', 'main')
    git(repo, 'branch', 'new')
    error('source_conflict', lambda: s.execute(p))
    s = EngineeringOperations(repository=TrustedRepository(str(repo)), check_write=lambda: True,
                              consume_approval=lambda _: True)
    error('frozen', lambda: s.execute(s.propose_branch('blocked', 'main')))


def test_real_ff_merge_does_not_checkout(repo):
    s = ops(repo, consume_approval=lambda _: True)
    error('active_branch', lambda: s.propose_merge('main', 'target'))
    p = s.propose_merge('target', 'main')
    assert '-first' in p.payload['diff'] and '+second' in p.payload['diff']
    before = (repo / 'a.txt').read_bytes()
    s.execute(p)
    assert git(repo, 'rev-parse', 'target') == git(repo, 'rev-parse', 'main')
    assert git(repo, 'branch', '--show-current') == 'main'
    assert (repo / 'a.txt').read_bytes() == before


@pytest.mark.parametrize('branch', ['--help', '../main', 'main~1', 'a//b', 'a/-b', 'a;cmd'])
def test_branch_injection_rejected(repo, branch):
    error('invalid_branch', lambda: ops(repo).propose_branch(branch, 'main'))


def test_repository_binding_and_remote_config(repo):
    child = repo / 'sub'
    child.mkdir()
    error('invalid_config', lambda: ops(child))
    error('invalid_config', lambda: EngineeringOperations(repository=TrustedRepository('relative')))
    for url in ('file:///tmp/repo', 'https://user:pass@example.org/repo', 'ssh://example.org/repo'):
        error('invalid_config', lambda: EngineeringOperations(repository=TrustedRepository(str(repo), (url,))))
    error('not_connected', lambda: ops(repo).propose_push('main', 'https://example.org/repo'))


def test_falsey_push_confirmation_callback_is_reported_without_invocation(repo):
    class Confirm:
        def __bool__(self):
            return False
        def __call__(self, proposal):
            pytest.fail('Capability inspection must not request push approval')
    confirmation = Confirm()
    service = EngineeringOperations(repository=TrustedRepository(str(repo), ('https://example.org/project.git',)),
        confirm_push=confirmation)
    assert service.confirm_push is confirmation
    assert service.capabilities()['git_push']['status'] == 'configured'


def test_push_mock_transport_exact_lease_and_confirmation(repo, monkeypatch):
    remote = 'https://example.org/project.git'
    captured = []
    s = EngineeringOperations(repository=TrustedRepository(str(repo), (remote,)),
                              check_write=lambda: None, consume_approval=lambda _: True,
                              confirm_push=lambda _: True)
    original = s._git
    def fake(*args):
        if args[0] == 'ls-remote':
            return b''
        if args[0] == 'push':
            captured.append(args)
            return b''
        return original(*args)
    monkeypatch.setattr(s, '_git', fake)
    p = s.propose_push('main', remote)
    assert '+second' in p.payload['diff']
    s.execute(p)
    assert captured == [('push', '--porcelain', '--force-with-lease=refs/heads/main:', '--', remote,
                         p.payload['after'] + ':refs/heads/main')]
    s.confirm_push = lambda _: False
    p = s.propose_push('main', remote)
    error('push_confirmation_required', lambda: s.execute(p))
    assert len(captured) == 1
    error('approval_consumed', lambda: s.execute(p))


def test_failed_push_transport_is_in_doubt_and_cannot_replay(repo, monkeypatch):
    remote = 'https://example.org/project.git'
    calls = []
    service = EngineeringOperations(repository=TrustedRepository(str(repo), (remote,)),
        check_write=lambda: False, consume_approval=lambda proposal: True, confirm_push=lambda proposal: True)
    original = service._git
    def transport(*args):
        if args[0] == 'ls-remote':
            return b''
        if args[0] == 'push':
            calls.append(args)
            raise CodeWorkspaceError('git_failed', 'mock ambiguous timeout')
        return original(*args)
    monkeypatch.setattr(service, '_git', transport)
    proposal = service.propose_push('main', remote)
    error('in_doubt', lambda: service.execute(proposal))
    error('approval_consumed', lambda: service.execute(proposal))
    assert len(calls) == 1


def workspace(tmp_path):
    source, storage = tmp_path / 'source', tmp_path / 'storage'
    source.mkdir()
    storage.mkdir()
    (source / 'a.txt').write_bytes(b'before\n')
    return LocalWorkspace(str(source.resolve()), str(storage.resolve()))


def interrupted(s):
    p = s.create_candidate({'a.txt': 'after\n', 'b.txt': 'added\n'})
    original = s._write_source
    def fail(name, content, journal, callback):
        if name == 'b.txt':
            raise OSError('simulated interruption')
        original(name, content, journal, callback)
    s._write_source = fail
    error('recovery_required', lambda: s.apply(p['candidate_id'], p['source_hash'], p['candidate_hash']))
    s._write_source = original
    return p


def test_real_partial_recovery_exact_candidate_and_restart(tmp_path):
    s = workspace(tmp_path)
    interrupted(s)
    assert s.read_file('a.txt') == 'after\n'
    op = EngineeringOperations(workspace=s, check_write=lambda: None, consume_approval=lambda _: True)
    p = op.propose_recovery()
    assert p.payload['candidate']['unified_diff']
    assert s.journal.exists() and s.read_file('a.txt') == 'after\n'
    restarted = EngineeringOperations(workspace=s, check_write=lambda: None, consume_approval=lambda _: True)
    error('invalid_proposal', lambda: restarted.execute(p))
    result = op.execute(p)
    assert result['action'] == 'rolled_back'
    assert s.read_file('a.txt') == 'before\n' and not s.journal.exists()


def test_recovery_drift_and_corrupt_cleanup_blocked(tmp_path):
    s = workspace(tmp_path)
    interrupted(s)
    op = EngineeringOperations(workspace=s, check_write=lambda: None, consume_approval=lambda _: True)
    p = op.propose_recovery()
    (s.source / 'a.txt').write_bytes(b'external')
    error('source_conflict', lambda: op.execute(p))
    (s.source / 'a.txt').write_bytes(b'after\n')
    journal = s._load(s.journal)
    journal['temps'] = ['a.txt']
    s._save(s.journal, journal)
    error('storage_corrupt', op.propose_recovery)
    assert s.journal.exists()


def test_committed_recovery_finalizes_only_valid_snapshot(tmp_path):
    s = workspace(tmp_path)
    p = s.create_candidate({'a.txt': 'after\n'})
    record, before, after = s._record(p['candidate_id'])
    (s.source / 'a.txt').write_bytes(b'after\n')
    commit = s._snapshot(after, 'test committed interruption')
    s._save(s.journal, {'schema': 1, 'candidate_id': p['candidate_id'], 'before': record['before'],
                        'after': record['after'], 'temps': [], 'directories': [],
                        'phase': 'committed', 'commit': commit})
    op = EngineeringOperations(workspace=s, check_write=lambda: None, consume_approval=lambda _: True)
    proposal = op.propose_recovery()
    assert proposal.payload['recovery_action'] == 'finalize'
    assert op.execute(proposal)['action'] == 'finalized'
    assert s.read_file('a.txt') == 'after\n'


def test_git_argv_shell_false_and_env(repo, monkeypatch):
    s = ops(repo)
    real = subprocess.run
    seen = []
    def spy(command, **kwargs):
        seen.append((command, kwargs))
        return real(command, **kwargs)
    monkeypatch.setattr(subprocess, 'run', spy)
    s.propose_branch('new', 'main')
    assert seen
    for argv, kwargs in seen:
        assert isinstance(argv, list) and argv[:3] == ['git', '-C', str(repo)]
        assert kwargs['shell'] is False
        assert kwargs['env']['GIT_TERMINAL_PROMPT'] == '0'
        assert 'core.hooksPath=' in ' '.join(argv)
