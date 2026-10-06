"""Fail-closed contracts for CI/CD, deployment and production databases.

The module deliberately contains no provider, network or database client. A real
integration must be supplied by trusted server configuration and callbacks, then
bound to an exact digest/epoch/approval by a separate control-plane adapter.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import secrets
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping


class ProductionCapabilityError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ProductionProposal:
    capability: str
    proposal_id: str
    digest: str
    epoch: int
    expires_at: int
    payload: Mapping[str, Any]


class ProductionAdapters:
    """Trusted callback boundary; all capabilities are closed by default."""
    def __init__(self, *, deployment: Callable | None = None,
                 database: Callable | None = None,
                 epoch: Callable[[], int] | None = None,
                 approval: Callable[[ProductionProposal], bool] | None = None):
        for callback in (deployment, database, epoch, approval):
            if callback is not None and not callable(callback):
                raise ProductionCapabilityError('invalid_configuration', 'Trusted adapters must be callable')
        self.deployment = deployment
        self.database = database
        self.epoch = epoch if epoch is not None else (lambda: 0)
        self.approval = approval
        self._lock = threading.RLock()
        self._pending = {}
        self._providers = {}

    def _current_epoch(self):
        try:
            value = self.epoch()
        except Exception as exc:
            raise ProductionCapabilityError('control_unavailable', 'Safety epoch unavailable') from exc
        if type(value) is not int or value < 0:
            raise ProductionCapabilityError('control_unavailable', 'Safety epoch must be a nonnegative integer')
        return value

    def _register(self, capability, proposal_id, digest, expires_at, payload):
        if not isinstance(expires_at, int) or isinstance(expires_at, bool) or not time.time() < expires_at <= time.time() + 300:
            raise ProductionCapabilityError('invalid_proposal', 'Expiry must be within five minutes')
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ProductionCapabilityError('invalid_proposal', 'Artifact digest must be a SHA-256 hex value')
        epoch = self._current_epoch()
        identifier = secrets.token_hex(24)
        bound_digest = hashlib.sha256(json.dumps({'capability': capability, 'id': identifier,
            'epoch': epoch, 'expires_at': expires_at, 'artifact_digest': digest, 'payload': payload},
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        proposal = ProductionProposal(capability, identifier, bound_digest, epoch, expires_at,
                                      MappingProxyType(dict(payload, artifact_digest=digest)))
        with self._lock:
            now = time.time()
            self._pending = {key: value for key, value in self._pending.items()
                             if value.expires_at > now}
            self._providers = {key: value for key, value in self._providers.items() if key in self._pending}
            if len(self._pending) >= 256:
                raise ProductionCapabilityError('proposal_limit',
                    'Too many pending production proposals; wait for expiry or finish existing approvals')
            self._pending[identifier] = proposal
            self._providers[identifier] = self.deployment if capability == 'production_deployment' else self.database
        return proposal

    def capabilities(self):
        return {
            'ci_cd': {'status': 'configured' if callable(self.deployment) else 'not_connected'},
            'production_deployment': {'status': 'configured' if callable(self.deployment) else 'not_connected'},
            'production_database': {'status': 'configured' if callable(self.database) else 'not_connected'},
            'production_restore': {'status': 'configured' if callable(self.database) else 'not_connected'},
            'automatic_execution': False,
            'production_approval_required': True,
        }

    def propose_deployment(self, *, proposal_id: str, digest: str, expires_at: int,
                           target: str, artifact: str):
        if not callable(self.deployment):
            raise ProductionCapabilityError('not_connected', 'No trusted CI/CD deployment adapter configured')
        if not all(isinstance(value, str) and value.strip() and len(value) <= 1024
                   for value in (digest, target, artifact)):
            raise ProductionCapabilityError('invalid_proposal', 'Exact artifact digest, target and artifact are required')
        return self._register('production_deployment', proposal_id, digest, expires_at,
                              {'target': target, 'artifact': artifact})

    def propose_database(self, *, proposal_id: str, digest: str, expires_at: int,
                         database_id: str, migration: str, backup_id: str):
        if not callable(self.database):
            raise ProductionCapabilityError('not_connected', 'No trusted production database adapter configured')
        if not all(isinstance(value, str) and value.strip() and len(value) <= 1024
                   for value in (digest, database_id, migration, backup_id)):
            raise ProductionCapabilityError('invalid_proposal', 'Database, migration, backup and digest are required')
        return self._register('production_database', proposal_id, digest, expires_at,
                              {'database_id': database_id, 'migration': migration, 'backup_id': backup_id})

    def propose_restore(self, *, proposal_id: str, digest: str, expires_at: int,
                        database_id: str, backup_id: str, recovery_point: str,
                        backup_verification: str):
        """Bind an exact recovery target; provider must independently verify backup."""
        if not callable(self.database):
            raise ProductionCapabilityError('not_connected', 'No trusted production database adapter configured')
        values = (digest, database_id, backup_id, recovery_point, backup_verification)
        if not all(isinstance(value, str) and value.strip() and len(value) <= 1024 for value in values):
            raise ProductionCapabilityError('invalid_proposal', 'Exact backup verification and recovery point required')
        return self._register('production_restore', proposal_id, digest, expires_at,
                              {'database_id': database_id, 'backup_id': backup_id,
                               'recovery_point': recovery_point,
                               'backup_verification': backup_verification})

    def execute(self, proposal: ProductionProposal):
        if not isinstance(proposal, ProductionProposal):
            raise ProductionCapabilityError('invalid_proposal', 'Exact server-created proposal required')
        with self._lock:
            if self._pending.get(proposal.proposal_id) is not proposal:
                raise ProductionCapabilityError('invalid_proposal', 'Unknown, forged or consumed proposal')
            # Burn identity-validated proposals before epoch/expiry checks: a stale
            # proposal must not become executable if an epoch source rolls back.
            del self._pending[proposal.proposal_id]
            bound_provider = self._providers.pop(proposal.proposal_id, None)
            if proposal.epoch != self._current_epoch() or time.time() >= proposal.expires_at:
                raise ProductionCapabilityError('approval_expired', 'Safety epoch changed or proposal expired')
        if not callable(self.approval) or self.approval(proposal) is not True:
            raise ProductionCapabilityError('approval_required', 'Separate explicit production approval required')
        if proposal.epoch != self._current_epoch() or time.time() >= proposal.expires_at:
            raise ProductionCapabilityError('approval_expired', 'Approval was revoked while awaiting confirmation')
        callback = self.deployment if proposal.capability == 'production_deployment' else self.database
        if not callable(callback):
            raise ProductionCapabilityError('not_connected', 'Trusted production adapter is unavailable')
        if callback is not bound_provider:
            raise ProductionCapabilityError('adapter_changed', 'Provider changed; create a new proposal and approval')
        try:
            result = callback(proposal)
        except Exception as exc:
            # A transport failure cannot prove the remote action did not happen.
            raise ProductionCapabilityError('in_doubt',
                'Provider execution interrupted; inspect provider before retry') from exc
        try:
            current_epoch = self._current_epoch()
        except ProductionCapabilityError as exc:
            raise ProductionCapabilityError('in_doubt', 'Safety unavailable after provider execution; inspect provider before retry') from exc
        if proposal.epoch != current_epoch:
            raise ProductionCapabilityError('in_doubt', 'Safety changed during provider execution; inspect provider before retry')
        if (not isinstance(result, Mapping) or result.get('status') not in ('completed', 'accepted')
                or result.get('proposal_digest') != proposal.digest or not result.get('provider_evidence')):
            raise ProductionCapabilityError('unverified', 'Provider returned no proposal-bound execution evidence')
        return dict(result)
