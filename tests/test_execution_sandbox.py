"""Unit Docker commands are mocked; real execution is a separate explicit opt-in."""
from dataclasses import replace
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from longflow.code_workspace import CodeWorkspaceService
from longflow.local_workspace import LocalWorkspace
from longflow.execution_sandbox import (
    CommandResult, ExecutionSandboxService, PRESETS, SandboxConfig, _Abort,
)

IMAGE = 'example/python-tools@sha256:' + 'a' * 64


@pytest.fixture
def workspace(tmp_path):
    source, base = tmp_path / 'source', tmp_path / 'private'
    source.mkdir()
    base.mkdir()
    (source / 'main.py').write_text('answer = 1\n', encoding='utf-8')
    service = CodeWorkspaceService(source, server_owned_base=base)
    candidate = service.create_candidate({'main.py': 'answer = 2\n'})
    return source, base, service, candidate


@pytest.fixture
def config():
    return SandboxConfig(enabled=True, image=IMAGE, trusted_image_digest=True)


@pytest.fixture
def local_workspace(tmp_path):
    source, storage = tmp_path / 'source', tmp_path / 'storage'
    source.mkdir()
    storage.mkdir()
    (source / 'main.py').write_text('answer = 1\n', encoding='utf-8')
    service = LocalWorkspace(source, storage)
    candidate = service.create_candidate({'main.py': 'answer = 2\n'})
    return source, storage, service, candidate


class DockerRunner:
    """Only mocks the Docker CLI contract, not candidate execution."""
    def __init__(self, overrides=None, hook=None):
        self.calls = []
        self.overrides = overrides or {}
        self.hook = hook
        self.mounted = None
        self.mounted_content = None

    def __call__(self, argv, **kwargs):
        assert argv[0] == 'docker'
        assert kwargs['timeout'] > 0
        assert kwargs['max_output_bytes'] > 0
        kwargs['check_abort']()
        self.calls.append(argv)
        stage = argv[1]
        if stage == 'create':
            mount = argv[argv.index('--mount') + 1]
            self.mounted = Path(mount.split('src=', 1)[1].split(',dst=', 1)[0])
            self.mounted_content = (self.mounted / 'main.py').read_text(encoding='utf-8')
            assert self.mounted_content == 'answer = 2\n'
        if self.hook:
            self.hook(stage, kwargs)
        kwargs['check_abort']()
        result = self.overrides.get(stage)
        if isinstance(result, BaseException):
            raise result
        if result is not None:
            return result
        return CommandResult(0, {'info': 'linux\n', 'create': 'container-id\n',
                                  'wait': '0\n', 'logs': '1 passed\n'}.get(stage, ''))


def execute(workspace, config, runner, **kwargs):
    _, _, service, candidate = workspace
    return ExecutionSandboxService(service, config=config, command_runner=runner).execute(
        candidate['candidate_id'], kwargs.pop('preset', 'pytest'),
        expected_candidate_sha=kwargs.pop('expected_candidate_sha', candidate['candidate_sha']),
        check_frozen=kwargs.pop('check_frozen', lambda: False), **kwargs)


def test_disabled_by_default_never_dispatches(workspace):
    runner = Mock(side_effect=AssertionError('must not execute'))
    _, _, service, candidate = workspace
    result = ExecutionSandboxService(service, command_runner=runner).execute(
        candidate['candidate_id'], 'pytest', expected_candidate_sha=candidate['candidate_sha'],
        check_frozen=lambda: False)
    assert result['status'] == 'not_connected' and result['reason'] == 'disabled'
    assert not result['passed'] and not result['host_fallback']
    runner.assert_not_called()


@pytest.mark.parametrize('changes', [
    {'enabled': 'false'}, {'enabled': 1}, {'enabled': 0}, {'enabled': None},
    {'image': 'python:latest'}, {'image': 'python@sha256:' + 'a' * 63},
    {'trusted_image_digest': False}, {'trusted_image_digest': 1},
    {'user': '0:0'}, {'user': 'root'}, {'memory_bytes': 0},
    {'timeout_seconds': float('nan')}, {'cpus': float('inf')},
    {'pids_limit': 1.2}, {'max_output_bytes': True},
])
def test_invalid_config_does_not_execute(workspace, config, changes):
    with pytest.raises((ValueError, TypeError)):
        replace(config, **changes).validate()
    runner = Mock()
    result = execute(workspace, replace(config, **changes), runner)
    assert result['status'] == 'invalid_configuration'
    assert not result['passed']
    runner.assert_not_called()


@pytest.mark.parametrize('preset', ['sh', 'pytest -q', 'compileall;id', ['pytest'], None])
def test_no_arbitrary_argv_or_shell(workspace, config, preset):
    runner = Mock()
    result = execute(workspace, config, runner, preset=preset)
    assert result['status'] == 'denied' and result['reason'] == 'unsupported_preset'
    runner.assert_not_called()


@pytest.mark.parametrize('control', ['check_frozen', 'cancelled'])
@pytest.mark.parametrize('value', [None, 0, 1, '', 'false', []])
def test_nonboolean_control_values_fail_closed(workspace, config, control, value):
    runner = Mock()
    result = execute(workspace, config, runner, **{control: lambda: value})
    assert result['reason'] == 'control_unavailable'
    assert not result['passed']
    runner.assert_not_called()


@pytest.mark.parametrize('control', ['check_frozen', 'cancelled'])
def test_control_becomes_invalid_after_container_start_aborts_and_cleans_up(workspace, config, control):
    value = [False]
    def hook(stage, kwargs):
        if stage == 'start':
            value[0] = 'false'
    runner = DockerRunner(hook=hook)
    result = execute(workspace, config, runner, **{control: lambda: value[0]})
    assert result['status'] == 'inconclusive'
    assert result['reason'] == 'control_unavailable'
    assert result['passed'] is False
    assert result['cleanup_complete'] is True
    assert [argv[1] for argv in runner.calls] == ['info', 'create', 'start', 'stop', 'kill', 'rm']
    assert not runner.mounted.exists()


@pytest.mark.parametrize('control', ['check_frozen', 'cancelled'])
def test_control_becomes_invalid_during_cleanup_does_not_veto_removal(workspace, config, control):
    value = [False]
    def hook(stage, kwargs):
        if stage == 'stop':
            value[0] = None
    runner = DockerRunner(hook=hook)
    result = execute(workspace, config, runner, **{control: lambda: value[0]})
    assert result['status'] == 'inconclusive'
    assert result['reason'] == 'control_unavailable'
    assert result['passed'] is False
    assert result['cleanup_complete'] is True
    assert [argv[1] for argv in runner.calls][-3:] == ['stop', 'kill', 'rm']
    assert not runner.mounted.exists()


def test_local_workspace_candidate_executes_from_isolated_copy(local_workspace, config):
    source, _, service, candidate = local_workspace
    runner = DockerRunner()
    result = ExecutionSandboxService(service, config=config, command_runner=runner).execute(
        candidate['candidate_id'], 'pytest', expected_candidate_sha=candidate['candidate_hash'],
        check_frozen=lambda: False)
    assert result['passed']
    assert runner.mounted is not None and runner.mounted_content == 'answer = 2\n'
    assert source.joinpath('main.py').read_text(encoding='utf-8') == 'answer = 1\n'
    assert not runner.mounted.exists()


def test_local_workspace_tampered_expected_hash_is_denied(local_workspace, config):
    _, _, service, candidate = local_workspace
    runner = DockerRunner()
    result = ExecutionSandboxService(service, config=config, command_runner=runner).execute(
        candidate['candidate_id'], 'pytest', expected_candidate_sha='b' * 64,
        check_frozen=lambda: False)
    assert result['status'] == 'denied' and result['reason'] == 'candidate_conflict'
    assert [argv[1] for argv in runner.calls] == ['info']


def test_hardened_lifecycle_and_separate_readonly_copy(workspace, config, preset):
    runner = DockerRunner()
    result = execute(workspace, config, runner, preset=preset)
    assert result['passed'] and result['status'] == 'passed'
    assert result['exit_code'] == 0 and result['output'] == '1 passed\n'
    assert result['output_delivered'] and result['cleanup_complete']
    assert [argv[1] for argv in runner.calls] == [
        'info', 'create', 'start', 'logs', 'wait', 'stop', 'kill', 'rm']
    create = runner.calls[1]
    for option, value in {'--network': 'none', '--cap-drop': 'ALL',
                          '--security-opt': 'no-new-privileges', '--user': '65534:65534',
                          '--pids-limit': '64', '--memory': str(config.memory_bytes),
                          '--memory-swap': str(config.memory_bytes), '--cpus': '1',
                          '--pull': 'never', '--entrypoint': '/usr/local/bin/python3'}.items():
        assert create[create.index(option) + 1] == value
    assert '--read-only' in create
    assert create[-len(PRESETS[preset]):] == PRESETS[preset]
    assert create[create.index(config.image)] == IMAGE
    mount = create[create.index('--mount') + 1]
    assert mount.endswith(',dst=/candidate,readonly')
    assert create[create.index('--tmpfs') + 1].startswith('/tmp:rw,nosuid,nodev,noexec,size=')
    assert 'docker.sock' not in ' '.join(create)
    source, base, _, candidate = workspace
    assert runner.mounted.parent == base
    assert runner.mounted not in (source, Path(candidate['copy_path']))
    assert not runner.mounted.exists()
    assert (source / 'main.py').read_text() == 'answer = 1\n'
    assert (Path(candidate['copy_path']) / 'main.py').read_text() == 'answer = 2\n'


@pytest.mark.parametrize('result', [FileNotFoundError(), CommandResult(1, 'daemon unavailable'),
                                     CommandResult(0, 'windows\n')])
def test_unavailable_is_not_connected_never_host_fallback(workspace, config, result):
    runner = DockerRunner({'info': result})
    evidence = execute(workspace, config, runner)
    assert evidence['status'] == 'not_connected'
    assert not evidence['passed'] and not evidence['host_fallback']
    assert [a[1] for a in runner.calls] == ['info']


def test_candidate_version_binding_before_create(workspace, config):
    runner = DockerRunner()
    result = execute(workspace, config, runner, expected_candidate_sha='b' * 64)
    assert result['status'] == 'denied' and result['reason'] == 'candidate_conflict'
    assert [a[1] for a in runner.calls] == ['info']


def test_unknown_candidate(workspace, config):
    runner = DockerRunner()
    result = ExecutionSandboxService(workspace[2], config=config, command_runner=runner).execute(
        'unknown', 'pytest', expected_candidate_sha='a' * 64, check_frozen=lambda: False)
    assert result['reason'] == 'not_found' and not result['passed']


@pytest.mark.parametrize('stage', ['create', 'start', 'logs', 'wait'])
@pytest.mark.parametrize('control', ['frozen', 'cancelled'])
def test_controls_abort_each_stage_and_cleanup(workspace, config, stage, control):
    state = {'blocked': False, 'checks': 0}

    def callback():
        state['checks'] += 1
        return state['blocked']

    def hook(current, kwargs):
        if current == stage:
            state['blocked'] = True
            kwargs['check_abort']()

    runner = DockerRunner(hook=hook)
    kwargs = {'check_frozen': callback} if control == 'frozen' else {'cancelled': callback}
    result = execute(workspace, config, runner, **kwargs)
    assert result['status'] == control and not result['passed']
    assert result['cleanup_complete']
    assert [a[1] for a in runner.calls][-3:] == ['stop', 'kill', 'rm']
    assert state['checks'] >= 8
    assert not runner.mounted.exists()


def test_frozen_preflight_does_not_contact_docker(workspace, config):
    runner = Mock()
    result = execute(workspace, config, runner, check_frozen=lambda: True)
    assert result['status'] == 'frozen'
    runner.assert_not_called()


def test_callback_failure_is_fail_closed(workspace, config):
    runner = Mock()
    result = execute(workspace, config, runner, check_frozen=Mock(side_effect=RuntimeError()))
    assert result['reason'] == 'control_unavailable' and not result['passed']
    runner.assert_not_called()


@pytest.mark.parametrize('stage', ['create', 'start', 'logs', 'wait'])
def test_timeout_cleans_container_even_if_create_interrupted(workspace, config, stage):
    runner = DockerRunner({stage: _Abort('timed_out')})
    result = execute(workspace, config, runner)
    assert result['status'] == 'timed_out' and not result['passed']
    assert result['cleanup_complete']
    assert [a[1] for a in runner.calls][-3:] == ['stop', 'kill', 'rm']


@pytest.mark.parametrize('overrides, reason', [
    ({'logs': CommandResult(1, 'missing')}, 'output_unavailable'),
    ({'logs': CommandResult(0, 'partial', True)}, 'output_limit'),
    ({'logs': CommandResult(0, 'x' * 100)}, 'output_limit'),
    ({'wait': CommandResult(0, 'not an exit code')}, 'wait_failed'),
    ({'wait': CommandResult(1, '0')}, 'wait_failed'),
    ({'create': CommandResult(1)}, 'create_failed'),
    ({'start': CommandResult(1)}, 'start_failed'),
])
def test_missing_or_limited_evidence_never_passes(workspace, config, overrides, reason):
    result = execute(workspace, replace(config, max_output_bytes=64), DockerRunner(overrides))
    assert not result['passed'] and result['status'] == 'inconclusive'
    assert result['reason'] == reason and result['cleanup_complete']


def test_nonzero_actual_exit_fails_with_logs(workspace, config):
    result = execute(workspace, config, DockerRunner({'wait': CommandResult(0, '7\n')}))
    assert result['status'] == 'failed' and result['exit_code'] == 7
    assert result['output_delivered'] and not result['passed']


def test_cleanup_failure_retains_snapshot_and_never_passes(workspace, config):
    runner = DockerRunner({'rm': CommandResult(1, 'cannot remove')})
    result = execute(workspace, config, runner)
    assert not result['passed'] and not result['cleanup_complete']
    assert result['reason'] == 'cleanup_failed'
    assert runner.mounted.exists()  # Do not remove a possibly still-mounted tree.


def test_stop_failure_still_kills_and_removes(workspace, config):
    runner = DockerRunner({'stop': OSError(), 'kill': CommandResult(1)})
    result = execute(workspace, config, runner)
    assert result['passed'] and result['cleanup_complete']
    assert [a[1] for a in runner.calls][-3:] == ['stop', 'kill', 'rm']


def test_real_docker_opt_in(tmp_path):
    """Requires an operator-trusted local Linux image containing Python at the
    fixed entrypoint. No mocks, pulls, installs, or daemon startup are performed.
    """
    if os.environ.get('LONGFLOW_REAL_DOCKER_TEST') != '1':
        pytest.skip('Real Docker execution requires LONGFLOW_REAL_DOCKER_TEST=1')
    image = os.environ.get('LONGFLOW_SANDBOX_IMAGE', '')
    assert image, 'LONGFLOW_SANDBOX_IMAGE must contain a pinned local image'
    assert os.environ.get('LONGFLOW_SANDBOX_TRUST_DIGEST') == '1'
    cfg = SandboxConfig(enabled=True, image=image, trusted_image_digest=True)
    cfg.validate()
    source, base = tmp_path / 'source', tmp_path / 'private'
    source.mkdir()
    base.mkdir()
    (source / 'main.py').write_text('answer = 42\n', encoding='utf-8')
    workspace = CodeWorkspaceService(source, server_owned_base=base)
    candidate = workspace.create_candidate()
    sandbox = ExecutionSandboxService(workspace, config=cfg)
    evidence = sandbox.execute(candidate['candidate_id'], 'compileall',
                               expected_candidate_sha=candidate['candidate_sha'],
                               check_frozen=lambda: False)
    assert evidence['passed'], evidence
    assert evidence['exit_code'] == 0 and evidence['output_delivered']
    assert evidence['cleanup_complete']
    assert (source / 'main.py').read_text() == 'answer = 42\n'
    assert not (source / '__pycache__').exists()
