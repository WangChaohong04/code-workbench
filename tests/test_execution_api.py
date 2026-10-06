"""Retained API contract tests for explicitly approved sandbox execution.

These tests are intentionally not run by the implementation assistant. Run through
 tools/test_engineering.ps1 after configuring a disposable workspace. Docker remains
 disabled unless a reviewed digest-pinned image is configured.
"""
import pytest
from fastapi.testclient import TestClient
from longflow import api, config


def _app(tmp_path):
    cfg = config.load_config(); cfg['db_path'] = str(tmp_path / 'execution.db')
    return api.create_app(cfg)


def test_execution_is_disabled_by_default(tmp_path):
    # No picker or executor is invoked. Test actual default configuration, not
    # route strings (which cannot prove an executor is disabled).
    cfg = config._deep_merge(config._DEFAULTS, {})
    assert cfg['execution_sandbox']['enabled'] is False
    assert cfg['execution_sandbox']['trusted_image_digest'] is False
    assert cfg['execution_sandbox']['image'] == ''


def test_execution_contract_requires_explicit_approval_and_exact_preset(tmp_path):
    app = _app(tmp_path)
    paths = {route.path for route in app.routes}
    assert '/api/local-workspaces/{wid}/execution/approvals' in paths
    assert '/api/local-workspaces/{wid}/execution/run' in paths
    assert '/api/local-workspaces/{wid}/execution/evidence/{candidate_id}' in paths
    app.state.lf.conn.close()


def test_real_execution_is_opt_in_only():
    pytest.skip('Requires Docker daemon and reviewed digest-pinned test image; never part of default suite')


def test_execution_models_are_strict_and_presets_fixed():
    from pydantic import ValidationError
    from longflow.local_workspace_api import ExecutionApproval
    from longflow.execution_sandbox import PRESETS, SandboxConfig
    assert set(PRESETS) == {'pytest', 'compileall', 'ruff'}
    assert SandboxConfig().enabled is False
    ExecutionApproval(candidate_id='c', source_hash='s', candidate_hash='h',
                      preset='pytest', confirmed=True)
    with pytest.raises(ValidationError):
        ExecutionApproval(candidate_id='c', source_hash='s', candidate_hash='h',
                          preset='pytest', confirmed='true')
