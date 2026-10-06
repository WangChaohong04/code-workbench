"""Copy candidates never mutate authoritative code or restore data."""
from pathlib import Path
from unittest.mock import patch

import pytest

from longflow.code_workspace import CodeWorkspaceError, CodeWorkspaceService


@pytest.fixture
def workspace(tmp_path):
    source = tmp_path / 'source'
    base = tmp_path / 'server-owned'
    source.mkdir()
    base.mkdir()
    (source / 'main.py').write_text('answer = 1\n', encoding='utf-8')
    (source / 'config').mkdir()
    (source / 'config' / 'app.yaml').write_text('mode: local\n', encoding='utf-8')
    return source, base, CodeWorkspaceService(source, server_owned_base=base)


def assert_error(code, operation):
    with pytest.raises(CodeWorkspaceError) as caught:
        operation()
    assert caught.value.code == code


def test_default_is_isolated_copy_with_manifest(workspace):
    source, base, service = workspace
    result = service.create_candidate()
    copy = Path(result['copy_path'])
    assert copy.parent == base
    assert copy != source
    assert (copy / 'main.py').read_bytes() == (source / 'main.py').read_bytes()
    assert result['base_sha'] == result['candidate_sha']
    assert len(result['base_manifest']['main.py']['sha256']) == 64
    assert result['unified_diff'] == ''
    assert result['mode'] == 'copy'
    assert not result['execution_supported'] and not result['apply_supported']


def test_creation_failure_never_falls_back(workspace):
    source, _, service = workspace
    with patch('longflow.code_workspace.tempfile.mkdtemp', side_effect=PermissionError):
        assert_error('sandbox_unavailable', lambda: service.create_candidate({'main.py': 'bad'}))
    assert (source / 'main.py').read_text() == 'answer = 1\n'
    assert service._candidates == {}


def test_unavailable_and_overlapping_base(workspace, tmp_path):
    source, _, _ = workspace
    assert_error('sandbox_unavailable', lambda: CodeWorkspaceService(source, server_owned_base=tmp_path / 'missing'))
    assert_error('sandbox_unavailable', lambda: CodeWorkspaceService(source, server_owned_base=None))
    assert_error('unsafe_path', lambda: CodeWorkspaceService(source, server_owned_base=source))
    child = source / 'candidate'
    child.mkdir()
    assert_error('unsafe_path', lambda: CodeWorkspaceService(source, server_owned_base=child))


@pytest.mark.parametrize('name', ['../escape.py', '/escape.py', 'a/../../escape',
    'C:/escape', 'a\\escape', 'a//b', './main.py', 'a/../b', 'a:stream',
    'NUL', 'folder/COM1.txt', 'a./b', '.git/config', 'data/app.db',
    'knowledge/rules.md', '.env', 'config/.env.secret', 'custom.sqlite3'])
def test_unsafe_candidate_paths(workspace, name):
    source, _, service = workspace
    assert_error('unsafe_path', lambda: service.create_candidate({name: 'bad'}))
    assert (source / 'main.py').read_text() == 'answer = 1\n'


def test_excludes_authoritative_and_sensitive_content(workspace):
    source, _, _ = workspace
    for name in ('.git', 'data', 'knowledge', 'private'):
        (source / name).mkdir()
        (source / name / 'secret').write_text('secret')
    (source / '.env').write_text('secret')
    service = CodeWorkspaceService(source, server_owned_base=workspace[1], excluded_paths=['private'])
    result = service.create_candidate()
    assert set(result['base_manifest']) == {'main.py', 'config/app.yaml'}
    assert result['versions']['data'] == {'status': 'excluded', 'restore_supported': False}


def test_write_delete_diff_and_original_remains_unchanged(workspace):
    source, _, service = workspace
    first = service.create_candidate({'main.py': 'answer = 2\n', 'new.py': 'new', 'config/app.yaml': None})
    assert '--- a/main.py\n+++ b/main.py\n' in first['unified_diff']
    assert '-answer = 1' in first['unified_diff']
    assert '+answer = 2' in first['unified_diff']
    assert '\\ No newline at end of file' in first['unified_diff']
    assert first['base_sha'] != first['candidate_sha']
    assert (source / 'main.py').read_text() == 'answer = 1\n'
    assert (source / 'config/app.yaml').exists()
    assert not (source / 'new.py').exists()
    assert first['versions']['code']['base_sha'] != first['versions']['code']['candidate_sha']
    assert first['versions']['config']['base_sha'] != first['versions']['config']['candidate_sha']


def test_preflight_requires_binding_and_remains_preview_only(workspace):
    source, _, service = workspace
    result = service.create_candidate({'main.py': 'answer = 2\n'})
    check = service.preflight_apply(result['candidate_id'], expected_source_sha=result['base_sha'],
                                    approved_candidate_sha=result['candidate_sha'])
    assert check['preflight_passed'] and not check['apply_supported']
    assert check['reason'] == 'no_reliable_apply_executor'
    assert (source / 'main.py').read_text() == 'answer = 1\n'
    for approved in ('', None, result['base_sha']):
        assert_error('approval_mismatch', lambda: service.preflight_apply(result['candidate_id'],
            expected_source_sha=result['base_sha'], approved_candidate_sha=approved))


@pytest.mark.parametrize('change', ['edit', 'add', 'delete', 'config'])
def test_source_version_conflicts(workspace, change):
    source, _, service = workspace
    result = service.create_candidate({'main.py': 'candidate'})
    if change == 'edit':
        (source / 'main.py').write_text('changed')
    elif change == 'add':
        (source / 'new.py').write_text('new')
    elif change == 'config':
        (source / 'config/app.yaml').write_text('changed')
    else:
        (source / 'main.py').unlink()
    assert_error('source_conflict', lambda: service.preflight_apply(result['candidate_id'],
        expected_source_sha=result['base_sha'], approved_candidate_sha=result['candidate_sha']))


def test_wrong_expected_source_and_stale_approval_rejected(workspace):
    _, _, service = workspace
    first = service.create_candidate({'main.py': 'v2'})
    assert_error('source_conflict', lambda: service.preflight_apply(first['candidate_id'],
        expected_source_sha='incorrect', approved_candidate_sha=first['candidate_sha']))
    second = service.update_candidate(first['candidate_id'], {'main.py': 'v3'}, expected_candidate_sha=first['candidate_sha'])
    assert second['candidate_sha'] != first['candidate_sha']
    assert second['base_sha'] == first['base_sha']
    assert_error('approval_mismatch', lambda: service.preflight_apply(first['candidate_id'],
        expected_source_sha=first['base_sha'], approved_candidate_sha=first['candidate_sha']))
    assert_error('candidate_conflict', lambda: service.update_candidate(first['candidate_id'],
        {'main.py': 'v4'}, expected_candidate_sha=first['candidate_sha']))


def test_manifest_output_is_not_authoritative(workspace):
    _, _, service = workspace
    result = service.create_candidate()
    result['base_manifest'].clear()
    assert 'main.py' in service.preview(result['candidate_id'])['base_manifest']
    assert_error('not_found', lambda: service.preview('../source'))


def test_case_and_file_directory_collisions(workspace):
    _, _, service = workspace
    for changes in ({'MAIN.py': 'bad'}, {'main.py/child': 'bad'},
                    {'Config/other.yaml': 'bad'}):
        assert_error('unsafe_path', lambda: service.create_candidate(changes))


def test_reparse_attribute_is_rejected(workspace):
    # Deterministic Windows-junction check without requiring junction privileges.
    import longflow.code_workspace as module
    from types import SimpleNamespace
    source, _, service = workspace
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == source / 'main.py':
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
        return value
    with patch.object(module.Path, 'lstat', lstat):
        assert_error('unsafe_path', service.create_candidate)


def test_symlink_rejected_in_source_and_candidate(workspace, tmp_path):
    source, _, service = workspace
    outside = tmp_path / 'outside.py'
    outside.write_text('outside')
    link = source / 'link.py'
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip('Host does not permit symlink creation; reparse guard separately tested')
    assert_error('unsafe_path', service.create_candidate)
    link.unlink()
    candidate = service.create_candidate()
    (Path(candidate['copy_path']) / 'link.py').symlink_to(outside)
    assert_error('unsafe_path', lambda: service.preview(candidate['candidate_id']))


def test_limits_fail_closed(workspace):
    source, base, _ = workspace
    service = CodeWorkspaceService(source, server_owned_base=base, max_total_bytes=1)
    assert_error('resource_limit', service.create_candidate)
    assert not list(base.iterdir())


def test_copy_tampering_invalidates_approval(workspace):
    _, _, service = workspace
    result = service.create_candidate({'main.py': 'candidate'})
    (Path(result['copy_path']) / 'main.py').write_text('tampered')
    assert_error('approval_mismatch', lambda: service.preflight_apply(result['candidate_id'],
        expected_source_sha=result['base_sha'], approved_candidate_sha=result['candidate_sha']))
