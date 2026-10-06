"""Local association, isolated Git history, durable candidates and recovery."""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from longflow.code_workspace import CodeWorkspaceError
from longflow.local_workspace import LocalWorkspace


@pytest.fixture
def workspace(tmp_path):
    source, storage = tmp_path / 'source', tmp_path / 'storage'
    source.mkdir()
    storage.mkdir()
    (source / 'main.py').write_bytes(b'answer = 1\n')
    (source / 'other.py').write_bytes(b'untouched\n')
    return source, storage, LocalWorkspace(source, storage)


def error(code, operation):
    with pytest.raises(CodeWorkspaceError) as caught:
        operation()
    assert caught.value.code == code


def apply(service, candidate, check_write=lambda: None):
    return service.apply(candidate['candidate_id'], candidate['source_hash'],
                         candidate['candidate_hash'], check_write)


def test_baseline_readonly_external_repository(workspace):
    source, storage, service = workspace
    assert not (source / '.git').exists()
    assert (storage / 'snapshots.git' / 'HEAD').exists()
    assert service.list_files() == ['main.py', 'other.py']
    assert service.read_file('main.py') == 'answer = 1\n'
    baseline = service.versions()
    assert len(baseline) == 1 and baseline[0]['timestamp']
    assert service.read_version(baseline[0]['commit'], 'main.py') == 'answer = 1\n'
    assert baseline[0]['source_hash'] == service.info()['source_hash']
    old_times = {p: p.stat().st_mtime_ns for p in source.iterdir()}
    candidate = service.create_candidate({'main.py': 'answer = 2\n'})
    service.preview(candidate['candidate_id'])
    service.restore_candidate(baseline[0]['commit'])
    assert {p: p.stat().st_mtime_ns for p in source.iterdir()} == old_times


def test_apply_add_delete_versions_restore_preview(workspace):
    source, storage, service = workspace
    baseline = service.versions()[0]['commit']
    candidate = service.create_candidate({'main.py': 'answer = 2\n', 'other.py': None,
                                           'pkg/new.py': 'hello\n', 'empty.py': ''})
    assert candidate['source_hash'] != candidate['candidate_hash']
    assert candidate['changed_files'] == ['empty.py', 'main.py', 'other.py', 'pkg/new.py']
    assert '--- /dev/null\n+++ b/empty.py\n' in candidate['unified_diff']
    assert '+answer = 2' in candidate['unified_diff']
    calls = []
    result = apply(service, candidate, lambda: calls.append(True))
    assert len(calls) >= 8 and result['atomic'] is False
    assert service.read_file('main.py') == 'answer = 2\n'
    assert not (source / 'other.py').exists()
    assert service.read_file('pkg/new.py') == 'hello\n'
    assert len(service.versions()) == 2
    difference = service.diff_versions(baseline, result['commit'])
    assert difference['unified_diff'] == candidate['unified_diff']
    restore = service.restore_candidate(baseline)
    assert restore['restored_commit'] == baseline
    assert 'other.py' in restore['after_manifest']
    assert service.read_file('main.py') == 'answer = 2\n'
    apply(service, restore)
    assert service.read_file('main.py') == 'answer = 1\n'
    assert not (source / 'pkg/new.py').exists()
    assert len(service.versions()) == 3


def test_candidates_survive_restart_and_association_is_bound(workspace, tmp_path):
    source, storage, service = workspace
    candidate = service.create_candidate({'main.py': 'persisted'})
    restarted = LocalWorkspace(source, storage)
    assert restarted.preview(candidate['candidate_id']) == candidate
    apply(restarted, candidate)
    other = tmp_path / 'other'
    other.mkdir()
    error('workspace_mismatch', lambda: LocalWorkspace(other, storage))
    error('unsafe_path', lambda: LocalWorkspace(source, source))


@pytest.mark.parametrize('drift', ['edit', 'add', 'delete'])
def test_entire_source_drift_blocks_apply(workspace, drift):
    source, _, service = workspace
    candidate = service.create_candidate({'main.py': 'candidate'})
    if drift == 'edit':
        (source / 'other.py').write_text('external')
    elif drift == 'add':
        (source / 'external.py').write_text('external')
    else:
        (source / 'other.py').unlink()
    error('source_conflict', lambda: apply(service, candidate))
    assert service.read_file('main.py') == 'answer = 1\n'
    assert not service.recovery_status()['required']


def test_exact_hashes_and_stored_candidate_tamper(workspace):
    _, storage, service = workspace
    candidate = service.create_candidate({'main.py': 'candidate'})
    error('candidate_conflict', lambda: service.apply(candidate['candidate_id'], candidate['source_hash'], 'wrong'))
    error('source_conflict', lambda: service.apply(candidate['candidate_id'], 'wrong', candidate['candidate_hash']))
    path = storage / 'candidates' / (candidate['candidate_id'] + '.json')
    record = json.loads(path.read_text())
    record['after']['main.py'] = 'tampered'
    path.write_text(json.dumps(record))
    error('candidate_conflict', lambda: service.preview(candidate['candidate_id']))
    assert service.read_file('main.py') == 'answer = 1\n'


@pytest.mark.parametrize('path', ['../outside', '/outside', 'a\\b', 'a:stream', 'NUL',
    'folder/COM1.txt', '.git/config', 'node_modules/a', 'venv/a', '.env',
    'secrets/a', 'credentials.json', 'config/key.pem', 'a//b', 'a./b'])
def test_paths_fail_closed(workspace, path):
    _, _, service = workspace
    error('unsafe_path', lambda: service.create_candidate({path: 'bad'}))


def test_exclusions_never_versioned_source_git_untouched(workspace):
    source, _, service = workspace
    for directory in ('.git', 'node_modules', 'venv', 'secrets'):
        (source / directory).mkdir()
        (source / directory / 'private').write_bytes(b'\x00secret')
    (source / '.env').write_text('secret')
    before = (source / '.git/private').read_bytes()
    candidate = service.create_candidate({'main.py': 'new'})
    assert set(candidate['before_manifest']) == {'main.py', 'other.py'}
    apply(service, candidate)
    assert (source / '.git/private').read_bytes() == before
    assert '.env' not in service.list_files()


def test_encoding_limits_and_collisions(workspace):
    source, storage, service = workspace
    for changes in ({'main.py': '\x00binary'}, {'MAIN.py': 'collision'}, {'main.py/child': 'bad'}):
        error('invalid_encoding' if '\x00binary' in changes.values() else 'unsafe_path',
              lambda: service.create_candidate(changes))
    (source / 'binary').write_bytes(b'\xff')
    error('invalid_encoding', service.list_files)
    (source / 'binary').unlink()
    error('resource_limit', lambda: service.create_candidate({'large': 'a' * (1024 * 1024 + 1)}))


def test_symlink_hardlink_and_reparse_guards(workspace, tmp_path):
    source, _, service = workspace
    outside = tmp_path / 'outside'
    outside.write_text('outside')
    link = source / 'link.py'
    try:
        link.symlink_to(outside)
    except OSError:
        pass
    else:
        error('unsafe_path', service.list_files)
        link.unlink()
    try:
        os.link(outside, link)
    except OSError:
        pass
    else:
        error('unsafe_path', service.list_files)
        link.unlink()
    from types import SimpleNamespace
    original = Path.lstat

    def reparse(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == source / 'main.py':
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
        return value
    with patch.object(Path, 'lstat', reparse):
        error('unsafe_path', service.list_files)


def test_freeze_failure_durable_explicit_recovery(workspace):
    source, storage, service = workspace
    candidate = service.create_candidate({'main.py': 'new', 'pkg/new.py': 'new'})
    calls = 0

    def freeze():
        nonlocal calls
        calls += 1
        return calls >= 4
    error('recovery_required', lambda: apply(service, candidate, freeze))
    assert service.recovery_status()['required']
    restarted = LocalWorkspace(source, storage)
    error('recovery_required', lambda: apply(restarted, candidate))
    assert restarted.recover()['action'] == 'rolled_back'
    assert restarted.read_file('main.py') == 'answer = 1\n'
    assert not (source / 'pkg').exists()
    assert len(restarted.versions()) == 1
    apply(restarted, candidate)


def test_recovery_refuses_external_drift_and_frozen_recovery(workspace):
    source, _, service = workspace
    candidate = service.create_candidate({'main.py': 'new'})
    calls = 0

    def freeze():
        nonlocal calls
        calls += 1
        return calls >= 3
    error('recovery_required', lambda: apply(service, candidate, freeze))
    error('frozen', lambda: service.recover(lambda: True))
    (source / 'other.py').write_text('external')
    error('source_conflict', service.recover)
    assert service.recovery_status()['required']


def test_snapshot_failure_after_write_has_explicit_recovery(workspace):
    _, _, service = workspace
    candidate = service.create_candidate({'main.py': 'new'})
    with patch.object(service, '_snapshot', side_effect=RuntimeError('git failed')):
        error('recovery_required', lambda: apply(service, candidate))
    assert service.read_file('main.py') == 'new'
    service.recover()
    assert service.read_file('main.py') == 'answer = 1\n'


def test_git_argument_arrays_and_isolated_environment(workspace):
    _, storage, service = workspace
    actual = __import__('subprocess').run
    calls = []

    def capture(command, **kwargs):
        calls.append((command, kwargs))
        return actual(command, **kwargs)
    with patch('longflow.local_workspace.subprocess.run', capture), patch.dict(os.environ, {
        'GIT_DIR': 'malicious', 'GIT_CONFIG_GLOBAL': 'malicious', 'GIT_CONFIG_COUNT': '1',
        'GIT_CONFIG_KEY_0': 'core.hooksPath', 'GIT_CONFIG_VALUE_0': 'malicious'}):
        service.create_candidate({'main.py': 'candidate'})
        service.versions()
    assert calls
    for command, kwargs in calls:
        assert isinstance(command, list) and kwargs['shell'] is False
        assert kwargs['cwd'] == storage and kwargs['capture_output'] is True
        assert kwargs['env']['GIT_CONFIG_NOSYSTEM'] == '1'
        assert kwargs['env']['GIT_CONFIG_GLOBAL'] == os.devnull
        assert 'GIT_DIR' not in kwargs['env'] and 'GIT_CONFIG_KEY_0' not in kwargs['env']
        assert 'core.fsmonitor=false' in command
        assert any(x.startswith('core.hooksPath=') for x in command)
