"""Opt-in engineering capabilities, deliberately NOT wired into HTTP or workers.

The trusted control plane selects the repository, serializes it against other
writers, authenticates/atomically consumes approvals, and implements freeze and
permission checks. Digests are bindings, NOT authentication. Proposals are only
valid in this instance; restart requires a new preview and approval. No deploy,
SQL execution, shell templates, automatic recovery or source-Git discovery.

Git supports branch creation and fast-forward updates of *inactive* branches.
No checkout, hooks, candidate execution, conflict resolution, or arbitrary merge
strategy. A separate worktree/full merge adapter is intentionally not provided.
LocalWorkspace's external snapshot history is never used as a native Git root.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .code_workspace import CodeWorkspaceError, _checked, _digest


def _fail(code, message):
    raise CodeWorkspaceError(code, message)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


@dataclass(frozen=True)
class TrustedRepository:
    """Server-owned configuration; never construct from an untrusted request.

    Native absolute worktree root and exact HTTPS remote URL allowlist. The root
    must not be the independent LocalWorkspace snapshot repository.
    """
    root: str
    remote_allowlist: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActionProposal:
    proposal_id: str
    action: str
    payload_json: str
    epoch: int
    expires_at: float
    digest: str

    @property
    def payload(self):
        return json.loads(self.payload_json)  # independent copy; immutable binding

    def envelope(self):
        return {'proposal_id': self.proposal_id, 'action': self.action,
                'payload': self.payload, 'epoch': self.epoch,
                'expires_at': self.expires_at}

    def to_dict(self):
        return {**self.envelope(), 'digest': self.digest}


class EngineeringOperations:
    def __init__(self, *, repository: TrustedRepository | None = None,
                 workspace=None, epoch=lambda: 0, check_write=None,
                 consume_approval=None, confirm_push=None, clock=time.time,
                 ttl_seconds=300):
        """Callbacks are trusted caller capabilities, not boolean request flags.

        consume_approval(proposal) must authenticate owner/workspace/action and
        atomically consume a single-use grant bound to the complete envelope and
        digest; return exactly True. confirm_push(proposal) is an independent,
        explicit user-confirmation adapter, default closed. check_write raises on
        forbidden/frozen writes (returning True also means frozen). Production
        adapters are absent, not simulated. Epoch is supplied by the safety layer.
        """
        if not isinstance(ttl_seconds, (int, float)) or isinstance(ttl_seconds, bool) or not 0 < ttl_seconds <= 300:
            _fail('invalid_config', 'Approval lifetime must be positive and at most 300 seconds')
        if not callable(epoch) or not callable(clock):
            _fail('invalid_config', 'Safety epoch and approval clock must be callable')
        for callback in (check_write, consume_approval, confirm_push):
            if callback is not None and not callable(callback):
                _fail('invalid_config', 'Trusted control adapters must be callable')
        self.repository, self.workspace = repository, workspace
        self.epoch, self.check_write = epoch, check_write
        self.consume_approval, self.confirm_push = consume_approval, confirm_push
        self.clock, self.ttl_seconds = clock, ttl_seconds
        self._lock = threading.RLock()
        self._pending = {}
        self._used = set()
        self.root = None
        self.remotes = ()
        if repository is not None:
            if not isinstance(repository, TrustedRepository):
                _fail('invalid_config', 'Require trusted repository configuration')
            root = Path(repository.root)
            if not root.is_absolute():
                _fail('invalid_config', 'Native repository root must be absolute')
            _checked(root)
            if not root.is_dir() or root.resolve() != root:
                _fail('invalid_config', 'Require a canonical native repository directory')
            if workspace is not None and (root == workspace.repo or root == workspace.storage or workspace.storage in root.parents):
                _fail('invalid_config', 'Independent snapshot storage is not a native repository')
            self.root = root
            self.remotes = tuple(repository.remote_allowlist)
            for remote in self.remotes:
                parsed = urlsplit(remote)
                if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                        or parsed.query or parsed.fragment or any(c.isspace() for c in remote)):
                    _fail('invalid_config', 'Only exact credential-free HTTPS remotes are supported')
            top = self._git('rev-parse', '--show-toplevel').decode().strip()
            if Path(top).resolve() != self.root or self._git('rev-parse', '--is-bare-repository').strip() != b'false':
                _fail('invalid_config', 'Configuration must name the exact native worktree root')
            gitdir = Path(self._git('rev-parse', '--absolute-git-dir').decode().strip())
            _checked(gitdir)
            if workspace is not None and (gitdir == workspace.repo or workspace.storage in gitdir.parents):
                _fail('invalid_config', 'Snapshot history cannot supply native Git metadata')

    def capabilities(self):
        git = 'configured' if self.root else 'not_connected'
        return {'git_branch': {'status': git},
                'git_merge': {'status': git, 'mode': 'fast_forward_inactive_ref_only'},
                'git_push': {'status': 'configured' if self.root and self.remotes and callable(self.confirm_push) else 'not_connected',
                             'reason': 'Requires exact HTTPS allowlist and explicit push confirmation adapter'},
                'local_recovery': {'status': 'configured' if self.workspace else 'not_connected'},
                'production_deployment': {'status': 'not_connected', 'reason': 'No production CI/CD adapter configured'},
                'production_database': {'status': 'not_connected', 'reason': 'No production database adapter configured'}}

    def production_deployment(self):
        return {'status': 'blocked', 'capability': 'production_deployment',
                'reason': 'No production CI/CD adapter configured; nothing executed'}

    def production_database(self):
        return {'status': 'blocked', 'capability': 'production_database',
                'reason': 'No production database adapter configured; nothing executed'}

    def _allow(self):
        if not callable(self.check_write):
            _fail('permission_denied', 'A trusted permission/freeze callback is required')
        try:
            result = self.check_write()
        except CodeWorkspaceError:
            raise
        except Exception:
            _fail('control_unavailable', 'Permission/freeze control unavailable')
        if result is True:
            _fail('frozen', 'Engineering writes frozen')
        if result is not None and result is not False:
            _fail('control_unavailable', 'Invalid permission/freeze control result')

    def _git(self, *args):
        if self.root is None:
            _fail('not_connected', 'No trusted native repository configured')
        _checked(self.root)
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
        env.update({'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_SYSTEM': os.devnull,
                    'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0',
                    'GIT_CONFIG_COUNT': '0', 'GIT_ATTR_NOSYSTEM': '1'})
        # Explicit argv, root, disabled hooks/helpers/external diffs. Git configuration
        # and metadata still belong to the trusted caller, not hostile local writers.
        command = ['git', '-C', str(self.root), '-c', 'core.hooksPath=' + os.devnull,
                   '-c', 'core.fsmonitor=false', '-c', 'core.attributesFile=' + os.devnull,
                   '-c', 'diff.external=', '-c', 'credential.helper=',
                   '-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always',
                   '-c', 'http.followRedirects=false', '-c', 'push.recurseSubmodules=no',
                   '-c', 'gc.auto=0', '-c', 'maintenance.auto=false']
        try:
            result = subprocess.run(command + list(args), cwd=self.root, env=env,
                                    input=b'', capture_output=True, shell=False, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CodeWorkspaceError('git_failed', 'Git unavailable or timed out; inspect state before retry') from exc
        if result.returncode:
            _fail('git_failed', 'Git operation failed; inspect state before proposing again')
        return result.stdout

    def _git_mutation(self, *args):
        try:
            return self._git(*args)
        except CodeWorkspaceError as exc:
            if exc.code != 'git_failed':
                raise
            raise CodeWorkspaceError('in_doubt',
                'Git mutation outcome uncertain; inspect local and remote state before retry') from exc

    @staticmethod
    def _branch(name):
        # Conservative subset, no revision expressions/options/ref traversal.
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_/-]{0,127}', name)
                or '//' in name or name.endswith('/') or any(p.startswith('-') for p in name.split('/'))):
            _fail('invalid_branch', 'Require a simple branch name')
        return 'refs/heads/' + name

    def _oid(self, ref):
        oid = self._git('rev-parse', '--verify', ref + '^{commit}').decode('ascii').strip()
        if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', oid):
            _fail('git_failed', 'Invalid native Git object identity')
        return oid

    def _refs(self):
        lines = self._git('for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads/').decode('ascii').splitlines()
        return dict(line.split(' ', 1) for line in lines)

    def _inactive(self, ref):
        records = self._git('worktree', 'list', '--porcelain').decode().splitlines()
        if 'branch ' + ref in records:
            _fail('active_branch', 'Target branch is checked out; use an isolated worktree adapter')

    def _diff(self, before, after):
        # --binary binds binary contents; --no-ext-diff/--no-textconv prohibit code.
        raw = self._git('diff', '--no-ext-diff', '--no-textconv', '--binary', '--full-index', before, after, '--')
        return {'diff': raw.decode('utf-8', errors='replace'), 'diff_sha256': hashlib.sha256(raw).hexdigest()}

    def _current_epoch(self):
        try:
            value = self.epoch()
        except Exception:
            _fail('control_unavailable', 'Safety epoch unavailable')
        if type(value) is not int or value < 0:
            _fail('control_unavailable', 'Safety epoch must be a nonnegative integer')
        return value

    def _now(self):
        try:
            value = self.clock()
            valid = type(value) in (int, float) and math.isfinite(value)
        except Exception:
            _fail('control_unavailable', 'Approval clock unavailable')
        if not valid:
            _fail('control_unavailable', 'Approval clock must be finite')
        return value

    def _proposal(self, action, payload):
        epoch = self._current_epoch()
        envelope = {'proposal_id': uuid.uuid4().hex, 'action': action, 'payload': payload,
                    'epoch': epoch, 'expires_at': self._now() + self.ttl_seconds}
        proposal = ActionProposal(envelope['proposal_id'], action, _json(payload), epoch,
                                  envelope['expires_at'], hashlib.sha256(_json(envelope).encode()).hexdigest())
        self._pending[proposal.proposal_id] = proposal
        return proposal

    def propose_branch(self, branch, base_branch):
        with self._lock:
            ref, base = self._branch(branch), self._branch(base_branch)
            if ref in self._refs():
                _fail('branch_exists', 'Target branch already exists')
            oid = self._oid(base)
            return self._proposal('git_branch', {'root': str(self.root), 'ref': ref, 'base_ref': base,
                                                'before': None, 'after': oid, **self._diff(oid, oid)})

    def propose_merge(self, target_branch, source_branch):
        with self._lock:
            target, source = self._branch(target_branch), self._branch(source_branch)
            self._inactive(target)
            before, after = self._oid(target), self._oid(source)
            if self._git('merge-base', before, after).decode().strip() != before:
                _fail('unsupported_merge', 'Only fast-forward merges are supported; no source checkout occurs')
            return self._proposal('git_merge', {'root': str(self.root), 'ref': target, 'source_ref': source,
                                               'before': before, 'after': after, **self._diff(before, after)})

    def _remote_oid(self, remote, ref):
        raw = self._git('ls-remote', '--refs', '--', remote, ref).decode('ascii').strip()
        if not raw:
            return None
        rows = raw.splitlines()
        if len(rows) != 1:
            _fail('remote_conflict', 'Remote response is ambiguous')
        oid, actual = rows[0].split('\t')
        if actual != ref or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', oid):
            _fail('remote_conflict', 'Invalid remote response')
        return oid

    def propose_push(self, branch, remote, remote_branch=None):
        with self._lock:
            if remote not in self.remotes or not self.confirm_push:
                _fail('not_connected', 'Push requires allowlisted remote and explicit confirmation adapter')
            ref = self._branch(branch)
            remote_ref = self._branch(remote_branch or branch)
            after = self._oid(ref)
            before = self._remote_oid(remote, remote_ref)
            if before is not None and self._git('merge-base', before, after).decode().strip() != before:
                _fail('unsupported_push', 'Remote tip must exist locally and be an ancestor; no force rewrites')
            # New branches disclose the entire tree against Git's empty tree.
            base = before or self._git('hash-object', '-t', 'tree', '--stdin').decode().strip()
            return self._proposal('git_push', {'root': str(self.root), 'ref': ref, 'remote': remote,
                                              'remote_ref': remote_ref, 'before': before, 'after': after,
                                              **self._diff(base, after)})

    def _recovery_state(self):
        s = self.workspace
        if s is None:
            _fail('not_connected', 'No local workspace recovery adapter configured')
        if not s.journal.exists():
            _fail('no_transaction', 'No interrupted transaction to recover')
        journal = s._load(s.journal)
        if journal.get('schema') != 1 or journal.get('phase') not in ('writing', 'committed'):
            _fail('storage_corrupt', 'Unknown transaction schema or phase')
        record, recorded_before, recorded_after = s._record(journal.get('candidate_id'))
        before, after = s._decode(journal.get('before')), s._decode(journal.get('after'))
        if before != recorded_before or after != recorded_after:
            _fail('storage_corrupt', 'Journal does not match original transaction candidate')
        # Validate cleanup metadata before any recovery can mutate source paths.
        for field in ('temps', 'directories'):
            if not isinstance(journal.get(field), list) or any(not isinstance(v, str) for v in journal[field]):
                _fail('storage_corrupt', 'Invalid cleanup metadata')
        for name in journal['temps']:
            if (not re.fullmatch(r'\.local-workspace-txn-[0-9a-f]{32}', Path(name).name)
                    or not any(Path(p).parent == Path(name).parent for p in before.keys() | after.keys()
                               if before.get(p) != after.get(p))):
                _fail('storage_corrupt', 'Cleanup path is not a related transaction temporary')
            s._target(name)
        from .code_workspace import _relative
        for name in journal['directories']:
            name = _relative(name)
            directory = s.source / name
            if name in before or not any(p.startswith(name + '/') for p in after):
                _fail('storage_corrupt', 'Cleanup directory is unrelated to transaction')
            if directory.exists():
                _checked(directory)
                if not directory.is_dir():
                    _fail('storage_corrupt', 'Cleanup directory is not a directory')
        current = s._scan(s.source)
        for name in before.keys() | after.keys() | current.keys():
            if (name in current, current.get(name)) not in ((name in before, before.get(name)), (name in after, after.get(name))):
                _fail('source_conflict', 'External drift prevents recovery preview')
        if journal['phase'] == 'committed':
            if current != after or s._version_files(journal.get('commit')) != after:
                _fail('source_conflict', 'Committed transaction source/snapshot mismatch')
        target = after if journal['phase'] == 'committed' else before
        return journal, current, target

    def propose_recovery(self):
        """Validate partially-applied state, then create an exact approval candidate.

        Does not repair files. Preview metadata creation itself requires permission.
        """
        with self._lock:
            self._allow()
            if self.workspace is None:
                _fail('not_connected', 'No local workspace configured')
            s = self.workspace
            with s._lock:
                journal, current, target = self._recovery_state()
                candidate = s._create(current, target)
                return self._proposal('local_recovery', {
                    'source': str(s.source), 'journal_digest': _digest(journal),
                    'transaction_candidate_id': journal['candidate_id'],
                    'recovery_action': 'finalize' if journal['phase'] == 'committed' else 'rollback',
                    'candidate': candidate})

    def _validate_state(self, proposal):
        p = proposal.payload
        if proposal.action == 'local_recovery':
            journal, current, target = self._recovery_state()
            s = self.workspace
            preview = s.preview(p['candidate']['candidate_id'])
            if (p['source'] != str(s.source) or _digest(journal) != p['journal_digest']
                    or preview != p['candidate'] or preview['source_hash'] != _digest(s._manifest(current))
                    or preview['candidate_hash'] != _digest(s._manifest(target))):
                _fail('source_conflict', 'Recovery state changed; create a new recovery preview')
            return
        if p['root'] != str(self.root):
            _fail('repository_conflict', 'Repository binding changed')
        if proposal.action == 'git_branch':
            if p['ref'] in self._refs() or self._oid(p['base_ref']) != p['after']:
                _fail('source_conflict', 'Branch proposal became stale')
        elif proposal.action == 'git_merge':
            self._inactive(p['ref'])
            if self._oid(p['ref']) != p['before'] or self._oid(p['source_ref']) != p['after']:
                _fail('source_conflict', 'Merge tips changed')
        elif proposal.action == 'git_push':
            if p['remote'] not in self.remotes or self._oid(p['ref']) != p['after'] or self._remote_oid(p['remote'], p['remote_ref']) != p['before']:
                _fail('source_conflict', 'Push state changed')
        else:
            _fail('invalid_proposal', 'Unsupported capability')

    def execute(self, proposal):
        """Never scheduled automatically. A caller must present the exact preview.

        Grants are burned on execution attempt even when downstream work fails.
        Network timeouts are ambiguous: inspect remote before a NEW proposal.
        """
        with self._lock:
            if not isinstance(proposal, ActionProposal) or self._pending.get(proposal.proposal_id) != proposal:
                _fail('invalid_proposal', 'Unknown or tampered proposal')
            if proposal.proposal_id in self._used:
                _fail('approval_consumed', 'Proposal already attempted')
            self._used.add(proposal.proposal_id)
            if proposal.epoch != self._current_epoch() or self._now() >= proposal.expires_at:
                _fail('approval_expired', 'Proposal expired or safety epoch changed')
            self._allow()
            # Hold workspace lock across preview validation, approval and recovery.
            lock = self.workspace._lock if proposal.action == 'local_recovery' else self._lock
            with lock:
                self._validate_state(proposal)
                if self.consume_approval is None:
                    _fail('approval_required', 'No authenticated approval consumer configured')
                if self.consume_approval(proposal) is not True:
                    _fail('approval_required', 'Exact single-use user approval required')
                if proposal.action == 'git_push' and (self.confirm_push is None or self.confirm_push(proposal) is not True):
                    _fail('push_confirmation_required', 'Explicit push confirmation required')
                self._allow()
                if proposal.epoch != self._current_epoch() or self._now() >= proposal.expires_at:
                    _fail('approval_expired', 'Approval changed while confirmation was pending')
                self._validate_state(proposal)
                p = proposal.payload
                if proposal.action == 'local_recovery':
                    result = self.workspace.recover(check_write=self._allow)
                elif proposal.action in ('git_branch', 'git_merge'):
                    old = p['before'] or '0' * len(p['after'])
                    self._git_mutation('update-ref', p['ref'], p['after'], old)
                    result = {'ref': p['ref'], 'before': p['before'], 'after': p['after']}
                else:
                    lease = '--force-with-lease=' + p['remote_ref'] + ':' + (p['before'] or '')
                    self._git_mutation('push', '--porcelain', lease, '--', p['remote'], p['after'] + ':' + p['remote_ref'])
                    result = {'remote': p['remote'], 'ref': p['remote_ref'], 'after': p['after']}
                # The mutation may already have happened; a revoked/unavailable
                # control source cannot justify a completed claim or blind retry.
                try:
                    self._allow()
                    current_epoch = self._current_epoch()
                except CodeWorkspaceError:
                    _fail('in_doubt', 'Safety unavailable after operation; inspect actual state before retry')
                if current_epoch != proposal.epoch:
                    _fail('in_doubt', 'Safety changed during operation; inspect actual state before retry')
                return {'status': 'completed', 'action': proposal.action, 'proposal_digest': proposal.digest, **result}
