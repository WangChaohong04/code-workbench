"""Opt-in Docker executor for server-owned candidates, never a host executor.

Configuration and callbacks belong to trusted control-plane code. Callers must
separately authorize execution; a candidate hash is not permission. The pinned
image digest must be explicitly trusted. Docker is not a guarantee against
kernel/daemon vulnerabilities. No Docker socket is mounted into containers.
Private candidate/base ownership is required, as documented in code_workspace.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
import shutil
import subprocess
import threading
import time
from types import MappingProxyType
import uuid

from .code_workspace import CodeWorkspaceError, CodeWorkspaceService, _digest


PRESETS = MappingProxyType({
    'pytest': ('-m', 'pytest', '-q', '-p', 'no:cacheprovider', '/candidate'),
    'compileall': ('-I', '-X', 'pycache_prefix=/tmp/pycache', '-m', 'compileall',
                   '-q', '/candidate'),
    'ruff': ('-m', 'ruff', 'check', '--isolated', '--no-cache', '/candidate'),
})


@dataclass(frozen=True)
class SandboxConfig:
    enabled: bool = False
    image: str = ''
    trusted_image_digest: bool = False
    user: str = '65534:65534'
    timeout_seconds: float = 60
    memory_bytes: int = 256 * 1024 * 1024
    cpus: float = 1
    pids_limit: int = 64
    tmpfs_bytes: int = 64 * 1024 * 1024
    max_output_bytes: int = 256 * 1024
    cleanup_timeout_seconds: float = 5

    def validate(self):
        if type(self.enabled) is not bool:
            raise ValueError('Sandbox enabled must be a boolean')
        if not re.fullmatch(r'[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}', self.image):
            raise ValueError('An explicit image pinned to a sha256 digest is required')
        if self.trusted_image_digest is not True:
            raise ValueError('The configured image digest must be explicitly trusted')
        if not re.fullmatch(r'[1-9][0-9]*:[1-9][0-9]*', self.user):
            raise ValueError('A numeric nonroot uid:gid is required')
        for value in (self.timeout_seconds, self.memory_bytes, self.cpus,
                      self.pids_limit, self.tmpfs_bytes, self.max_output_bytes,
                      self.cleanup_timeout_seconds):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError('All resource limits must be finite and positive')
        for value in (self.memory_bytes, self.pids_limit, self.tmpfs_bytes, self.max_output_bytes):
            if not isinstance(value, int):
                raise ValueError('Byte and process limits must be integers')


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str = ''
    truncated: bool = False


class _Abort(Exception):
    pass


def docker_command_runner(argv, *, timeout, max_output_bytes, check_abort):
    """Bounded argv-only CLI runner, polling controls during active commands.

    A reader drains a combined pipe without unbounded buffering. Killing a CLI
    does not kill its container; the service must perform container cleanup.
    """
    check_abort()
    process = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               shell=False)
    output = bytearray()
    overflow = threading.Event()

    def read_output():
        while True:
            chunk = process.stdout.read1(4096)
            if not chunk:
                break
            remaining = max_output_bytes - len(output)
            output.extend(chunk[:max(0, remaining)])
            if len(chunk) > remaining:
                overflow.set()

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            check_abort()
            if overflow.is_set():
                raise _Abort('output_limit')
            if time.monotonic() >= deadline:
                raise _Abort('timed_out')
            threading.Event().wait(.02)
        reader.join(timeout=1)
        if reader.is_alive():
            raise _Abort('output_unavailable')
        check_abort()
        return CommandResult(process.returncode, output.decode('utf-8', errors='replace'),
                             overflow.is_set())
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        reader.join(timeout=1)
        process.stdout.close()


class ExecutionSandboxService:
    def __init__(self, workspace: CodeWorkspaceService, *, config=None, command_runner=None):
        self.workspace = workspace
        self.config = config if config is not None else SandboxConfig()
        self.runner = command_runner if command_runner is not None else docker_command_runner

    def materialize_candidate(self, candidate_id, expected_hash):
        """Create an isolated copy of a trusted candidate revision.

        CodeWorkspaceService records point at a server-owned root. LocalWorkspace
        records are durable ``(record, before, after)`` tuples, so only that
        exact tuple shape is adapted here; the stored hash and reconstructed
        manifest must both agree with the caller's expected hash.
        """
        with self.workspace._lock:
            record = self.workspace._record(candidate_id)
            if isinstance(record, dict):
                files = self.workspace._scan(record['root'], candidate=True)
                if _digest(self.workspace._manifest(files)) != expected_hash:
                    raise CodeWorkspaceError('candidate_conflict', 'Candidate version changed')
                return self.workspace._new_copy(files)
            if type(record) is not tuple or len(record) != 3:
                raise CodeWorkspaceError('candidate_conflict', 'Unsupported candidate record')
            metadata, _before, after = record
            if (not isinstance(metadata, dict) or not isinstance(after, dict)
                    or metadata.get('candidate_hash') != expected_hash
                    or any(not isinstance(name, str) or not isinstance(content, bytes)
                           for name, content in after.items())):
                raise CodeWorkspaceError('candidate_conflict', 'Candidate version changed')
            if _digest(self.workspace._manifest(after)) != expected_hash:
                raise CodeWorkspaceError('candidate_conflict', 'Candidate version changed')
            return self.workspace._new_copy(after)

    def execute(self, candidate_id, preset, *, expected_candidate_sha,
                check_frozen, cancelled=None) -> dict:
        """JSON-ready execution evidence, accepting no user argv or host path.

        check_frozen() and optional cancelled() return booleans; exceptions fail
        closed. Both are polled throughout real CLI commands. Cleanup observes
        controls but cannot be vetoed. Injected runners are trusted/test-only and
        must honor the same timeout/output/check_abort contract.
        """
        cfg = self.config
        evidence = {'candidate_id': candidate_id, 'candidate_sha': expected_candidate_sha,
                    'preset': preset, 'image': cfg.image, 'status': 'not_connected',
                    'passed': False, 'exit_code': None, 'output': '',
                    'output_delivered': False, 'output_truncated': False,
                    'cleanup_complete': True, 'reason': '', 'container_name': None,
                    'boundary': 'docker', 'host_fallback': False}
        if type(cfg.enabled) is not bool:
            evidence.update(status='invalid_configuration', reason='Sandbox enabled must be a boolean')
            return evidence
        if cfg.enabled is False:
            evidence['reason'] = 'disabled'
            return evidence
        try:
            cfg.validate()
        except (ValueError, TypeError) as exc:
            evidence.update(status='invalid_configuration', reason=str(exc))
            return evidence
        if not isinstance(preset, str) or preset not in PRESETS:
            evidence.update(status='denied', reason='unsupported_preset')
            return evidence
        if not callable(check_frozen) or (cancelled is not None and not callable(cancelled)):
            evidence.update(status='denied', reason='control_callback_required')
            return evidence
        deadline = time.monotonic() + cfg.timeout_seconds
        snapshot = None
        container = None
        attempted_create = False

        def control_value(callback):
            value = callback()
            if type(value) is not bool:
                raise _Abort('control_unavailable')
            return value

        def controls():
            try:
                if control_value(check_frozen):
                    raise _Abort('frozen')
                if cancelled is not None and control_value(cancelled):
                    raise _Abort('cancelled')
            except _Abort:
                raise
            except Exception as exc:
                raise _Abort('control_unavailable') from exc
            if time.monotonic() >= deadline:
                raise _Abort('timed_out')

        def command(args, *, cleanup=False):
            if cleanup:
                # Observe freezes, but never let cancellation deny containment cleanup.
                def observe():
                    try:
                        if control_value(check_frozen) and evidence['status'] in ('passed', 'failed'):
                            evidence.update(status='frozen', reason='frozen')
                        if (cancelled is not None and control_value(cancelled)
                                and evidence['status'] in ('passed', 'failed')):
                            evidence.update(status='cancelled', reason='cancelled')
                    except Exception:
                        if evidence['status'] in ('passed', 'failed'):
                            evidence.update(status='inconclusive', reason='control_unavailable')
                callback = observe
                timeout = cfg.cleanup_timeout_seconds
            else:
                controls()
                callback = controls
                timeout = max(.001, deadline - time.monotonic())
            result = self.runner(('docker', *args), timeout=timeout,
                                 max_output_bytes=cfg.max_output_bytes, check_abort=callback)
            callback()
            if result.truncated or len(result.output.encode('utf-8')) > cfg.max_output_bytes:
                raise _Abort('output_limit')
            return result

        try:
            controls()
            available = command(('info', '--format', '{{.OSType}}'))
            if available.returncode != 0 or available.output.strip() != 'linux':
                evidence['reason'] = 'docker_unavailable_or_not_linux'
                return evidence
            with self.workspace._lock:
                controls()
                snapshot = self.materialize_candidate(candidate_id, expected_candidate_sha)
                # mkdtemp uses 0700; the isolated copy must be readable by the
                # configured nonroot container user, never the original tree.
                snapshot.chmod(0o755)
                for child in snapshot.rglob('*'):
                    child.chmod(0o755 if child.is_dir() else 0o644)
            controls()
            if ',' in str(snapshot):
                raise CodeWorkspaceError('unsafe_path', 'Docker mount path contains comma')
            container = 'longflow-exec-' + uuid.uuid4().hex
            evidence['container_name'] = container
            attempted_create = True  # Even an interrupted create can leave a container.
            create = command(('create', '--name', container, '--pull', 'never',
                '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--user', cfg.user,
                '--pids-limit', str(cfg.pids_limit), '--memory', str(cfg.memory_bytes),
                '--memory-swap', str(cfg.memory_bytes), '--cpus', str(cfg.cpus),
                '--ulimit', 'nofile=256:256', '--restart', 'no',
                '--log-driver', 'local', '--log-opt', 'max-size=1m', '--log-opt', 'max-file=1',
                '--mount', f'type=bind,src={snapshot},dst=/candidate,readonly',
                '--tmpfs', f'/tmp:rw,nosuid,nodev,noexec,size={cfg.tmpfs_bytes},mode=1777',
                '--workdir', '/candidate', '--env', 'HOME=/tmp',
                '--env', 'PYTHONDONTWRITEBYTECODE=1',
                '--env', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD=1',
                '--entrypoint', '/usr/local/bin/python3', cfg.image, *PRESETS[preset]))
            if create.returncode != 0:
                raise _Abort('create_failed')
            if command(('start', container)).returncode != 0:
                raise _Abort('start_failed')
            logs = command(('logs', '--follow', container))
            if logs.returncode != 0:
                raise _Abort('output_unavailable')
            evidence.update(output=logs.output, output_delivered=True)
            wait = command(('wait', container))
            if wait.returncode != 0 or not re.fullmatch(r'\d+\s*', wait.output):
                raise _Abort('wait_failed')
            evidence['exit_code'] = int(wait.output.strip())
            evidence['status'] = 'passed' if evidence['exit_code'] == 0 else 'failed'
            controls()
        except CodeWorkspaceError as exc:
            evidence.update(status='denied', reason=exc.code)
        except _Abort as exc:
            reason = str(exc)
            evidence.update(status=reason if reason in ('frozen', 'cancelled', 'timed_out')
                            else 'inconclusive', reason=reason,
                            output_truncated=reason == 'output_limit')
        except (OSError, subprocess.SubprocessError):
            evidence.update(status='not_connected' if not attempted_create else 'inconclusive',
                            reason='docker_unavailable')
        except Exception:
            evidence.update(status='inconclusive', reason='execution_error')
        finally:
            removed = not attempted_create
            if attempted_create:
                # stop/kill are best effort; rm -f is the decisive cleanup result.
                for args in (('stop', '--time', '0', container), ('kill', container),
                             ('rm', '--force', container)):
                    try:
                        result = command(args, cleanup=True)
                        if args[0] == 'rm':
                            removed = result.returncode == 0
                    except Exception:
                        pass
            evidence['cleanup_complete'] = removed
            if snapshot is not None and removed:
                try:
                    shutil.rmtree(snapshot)
                except OSError:
                    evidence['cleanup_complete'] = False
            if not evidence['cleanup_complete']:
                evidence.update(status='inconclusive', reason='cleanup_failed')
            evidence['passed'] = (evidence['status'] == 'passed'
                                  and evidence['exit_code'] == 0
                                  and evidence['output_delivered']
                                  and evidence['cleanup_complete'])
        return evidence
