"""Contract tests for fail-closed production adapter callbacks."""
import time
from dataclasses import replace
from types import MappingProxyType

import pytest

from longflow.production_adapters import (
    ProductionAdapters,
    ProductionCapabilityError,
    ProductionProposal,
)


def fresh_expiry():
    return int(time.time()) + 120
DIGEST = "a" * 64


def test_default_capabilities_are_not_connected_and_closed():
    adapters = ProductionAdapters()

    assert adapters.capabilities() == {
        "ci_cd": {"status": "not_connected"},
        "production_deployment": {"status": "not_connected"},
        "production_database": {"status": "not_connected"},
        "production_restore": {"status": "not_connected"},
        "automatic_execution": False,
        "production_approval_required": True,
    }


@pytest.mark.parametrize('operation', ['deployment', 'migration', 'restore'])
@pytest.mark.parametrize('stage', ['before_approval', 'during_approval'])
def test_changed_callable_provider_cannot_use_old_proposal(stage, operation):
    calls = []
    original = lambda proposal: calls.append('original') or {}
    replacement = lambda proposal: calls.append('replacement') or {}
    adapters = ProductionAdapters(deployment=original)
    def approve(proposal):
        if stage == 'during_approval':
            adapters.deployment = replacement
        return True
    adapters.approval = approve
    proposal = adapters.propose_deployment(proposal_id='ignored', digest=DIGEST,
        expires_at=fresh_expiry(), target='prod', artifact='image')
    if stage == 'before_approval':
        adapters.deployment = replacement
    with pytest.raises(ProductionCapabilityError) as error:
        adapters.execute(proposal)
    assert error.value.code == 'adapter_changed'
    assert calls == []
    assert proposal.proposal_id not in adapters._providers
    with pytest.raises(ProductionCapabilityError) as replay:
        adapters.execute(proposal)
    assert replay.value.code == 'invalid_proposal'


@pytest.mark.parametrize('field,code', [('deployment', 'not_connected'), ('approval', 'approval_required')])
@pytest.mark.parametrize('invalid', [True, 'callback', {}, []])
def test_callback_replaced_with_noncallable_is_rejected_and_proposal_consumed(field, code, invalid):
    calls = []
    adapters = ProductionAdapters(deployment=lambda proposal: calls.append(proposal) or {},
        approval=lambda proposal: True)
    proposal = adapters.propose_deployment(proposal_id='ignored', digest=DIGEST,
        expires_at=fresh_expiry(), target='prod', artifact='image')
    setattr(adapters, field, invalid)
    with pytest.raises(ProductionCapabilityError) as error:
        adapters.execute(proposal)
    assert error.value.code == code
    assert calls == []
    with pytest.raises(ProductionCapabilityError) as replay:
        adapters.execute(proposal)
    assert replay.value.code == 'invalid_proposal'


def test_falsey_callable_epoch_is_preserved_and_enforces_revocation():
    class Epoch:
        value = 7
        def __bool__(self):
            return False
        def __call__(self):
            return self.value
    epoch = Epoch()
    calls = []
    adapters = ProductionAdapters(epoch=epoch, approval=lambda proposal: True,
        deployment=lambda proposal: calls.append(proposal) or {})
    assert adapters.epoch is epoch
    proposal = adapters.propose_deployment(proposal_id='ignored', digest=DIGEST,
        expires_at=fresh_expiry(), target='prod', artifact='image')
    assert proposal.epoch == 7
    epoch.value = 8
    with pytest.raises(ProductionCapabilityError) as error:
        adapters.execute(proposal)
    assert error.value.code == 'approval_expired'
    assert calls == []


def test_falsey_provider_callbacks_are_reported_configured_without_invocation():
    class Provider:
        def __bool__(self):
            return False
        def __call__(self, proposal):
            pytest.fail('Capability inspection must never invoke provider')
    provider = Provider()
    capabilities = ProductionAdapters(deployment=provider, database=provider).capabilities()
    for name in ('ci_cd', 'production_deployment', 'production_database', 'production_restore'):
        assert capabilities[name]['status'] == 'configured'
    assert capabilities['automatic_execution'] is False


def test_capabilities_are_independent_for_deployment_and_database_callbacks():
    deployment_only = ProductionAdapters(deployment=lambda proposal: {})
    assert deployment_only.capabilities()["production_deployment"]["status"] == "configured"
    assert deployment_only.capabilities()["production_database"]["status"] == "not_connected"
    assert deployment_only.capabilities()["production_restore"]["status"] == "not_connected"

    database_only = ProductionAdapters(database=lambda proposal: {})
    assert database_only.capabilities()["production_deployment"]["status"] == "not_connected"
    assert database_only.capabilities()["production_database"]["status"] == "configured"
    assert database_only.capabilities()["production_restore"]["status"] == "configured"


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("propose_deployment", {"digest": "", "target": "prod", "artifact": "image"}),
        ("propose_deployment", {"digest": "A" * 64, "target": "prod", "artifact": "image"}),
        ("propose_deployment", {"digest": "a" * 63, "target": "prod", "artifact": "image"}),
        ("propose_deployment", {"digest": "a" * 1025, "target": "prod", "artifact": "image"}),
        ("propose_deployment", {"digest": DIGEST, "target": "   ", "artifact": "image"}),
        ("propose_deployment", {"digest": DIGEST, "target": "x" * 1025, "artifact": "image"}),
        ("propose_deployment", {"digest": DIGEST, "target": "prod", "artifact": ""}),
        ("propose_deployment", {"digest": DIGEST, "target": "prod", "artifact": "x" * 1025}),
        ("propose_database", {"digest": DIGEST, "database_id": "", "migration": "m1", "backup_id": "b1"}),
        ("propose_database", {"digest": DIGEST, "database_id": "main", "migration": "   ", "backup_id": "b1"}),
        ("propose_database", {"digest": DIGEST, "database_id": "main", "migration": "m1", "backup_id": ""}),
        ("propose_database", {"digest": DIGEST, "database_id": "d" * 1025, "migration": "m1", "backup_id": "b1"}),
    ],
)
def test_proposals_require_strict_fields(method, kwargs):
    adapters = ProductionAdapters(
        deployment=lambda proposal: {}, database=lambda proposal: {}
    )
    base = {"proposal_id": "caller-id", "expires_at": fresh_expiry(), **kwargs}

    with pytest.raises(ProductionCapabilityError) as exc_info:
        getattr(adapters, method)(**base)

    assert exc_info.value.code == "invalid_proposal"


@pytest.mark.parametrize("field", ["database_id", "backup_id", "recovery_point", "backup_verification"])
def test_restore_requires_nonempty_bounded_fields(field):
    adapters = ProductionAdapters(database=lambda proposal: {})
    values = {
        "database_id": "main", "backup_id": "backup-1",
        "recovery_point": "2025-01-01T00:00:00Z", "backup_verification": "verified",
    }
    values[field] = "   "

    with pytest.raises(ProductionCapabilityError) as exc_info:
        adapters.propose_restore(
            proposal_id="caller-id", digest=DIGEST, expires_at=fresh_expiry(), **values
        )
    assert exc_info.value.code == "invalid_proposal"

    values[field] = "x" * 1025
    with pytest.raises(ProductionCapabilityError) as exc_info:
        adapters.propose_restore(
            proposal_id="caller-id", digest=DIGEST, expires_at=fresh_expiry(), **values
        )
    assert exc_info.value.code == "invalid_proposal"


def test_restore_proposal_is_bound_and_executes_through_database_callback():
    received = []

    def callback(proposal):
        received.append(proposal)
        assert proposal.capability == "production_restore"
        assert proposal.payload["database_id"] == "main"
        assert proposal.payload["backup_id"] == "backup-1"
        assert proposal.payload["recovery_point"] == "2025-01-01T00:00:00Z"
        assert proposal.payload["backup_verification"] == "verified"
        return {
            "status": "completed", "proposal_digest": proposal.digest,
            "provider_evidence": {"backup": "backup-1", "verified": True},
        }

    adapters = ProductionAdapters(database=callback, approval=lambda proposal: True)
    proposal = adapters.propose_restore(
        proposal_id="caller-id", digest=DIGEST, expires_at=fresh_expiry(),
        database_id="main", backup_id="backup-1",
        recovery_point="2025-01-01T00:00:00Z", backup_verification="verified",
    )

    result = adapters.execute(proposal)

    assert result["status"] == "completed"
    assert result["proposal_digest"] == proposal.digest
    assert received == [proposal]


@pytest.mark.parametrize("expires_at", [True, fresh_expiry() + 301, int(time.time()) - 1])
def test_proposals_require_integer_future_expiry_within_five_minutes(expires_at):
    adapters = ProductionAdapters(deployment=lambda proposal: {})

    with pytest.raises(ProductionCapabilityError) as exc_info:
        adapters.propose_deployment(
            proposal_id="caller-id", digest=DIGEST, expires_at=expires_at,
            target="prod", artifact="image",
        )

    assert exc_info.value.code == "invalid_proposal"


def test_proposal_id_is_server_generated_and_payload_is_immutable():
    adapters = ProductionAdapters(deployment=lambda proposal: {})

    proposal = adapters.propose_deployment(
        proposal_id="caller-controlled", digest=DIGEST, expires_at=fresh_expiry(),
        target="prod", artifact="image",
    )

    assert proposal.proposal_id != "caller-controlled"
    assert isinstance(proposal.payload, MappingProxyType)
    assert proposal.payload["artifact_digest"] == DIGEST
    with pytest.raises(TypeError):
        proposal.payload["target"] = "other"


@pytest.mark.parametrize('invalid_epoch', [True, 1.0, -1, None, '1'])
def test_invalid_epoch_rejected_before_registration(invalid_epoch):
    adapters = ProductionAdapters(deployment=lambda proposal: {}, epoch=lambda: invalid_epoch)
    with pytest.raises(ProductionCapabilityError) as error:
        adapters.propose_deployment(proposal_id='ignored', digest=DIGEST,
            expires_at=fresh_expiry(), target='prod', artifact='image')
    assert error.value.code == 'control_unavailable'
    assert adapters._pending == {}


@pytest.mark.parametrize('stage', ['before_approval', 'during_approval', 'during_provider'])
@pytest.mark.parametrize('invalid_epoch', [True, 1.0, None, -1])
def test_invalid_execution_epoch_fails_closed_and_consumes_proposal(stage, invalid_epoch):
    current = [1]
    approvals, calls = [], []
    def approve(proposal):
        approvals.append(proposal)
        if stage == 'during_approval':
            current[0] = invalid_epoch
        return True
    def provider(proposal):
        calls.append(proposal)
        current[0] = invalid_epoch
        return {'status': 'completed', 'proposal_digest': proposal.digest,
                'provider_evidence': {'id': 'mock-run'}}
    adapters = ProductionAdapters(deployment=provider, approval=approve, epoch=lambda: current[0])
    proposal = adapters.propose_deployment(proposal_id='ignored', digest=DIGEST,
        expires_at=fresh_expiry(), target='prod', artifact='image')
    if stage == 'before_approval':
        current[0] = invalid_epoch
    with pytest.raises(ProductionCapabilityError) as error:
        adapters.execute(proposal)
    assert error.value.code == ('in_doubt' if stage == 'during_provider' else 'control_unavailable')
    assert len(approvals) == (0 if stage == 'before_approval' else 1)
    assert len(calls) == (1 if stage == 'during_provider' else 0)
    current[0] = 1
    with pytest.raises(ProductionCapabilityError) as replay:
        adapters.execute(proposal)
    assert replay.value.code == 'invalid_proposal'


def test_epoch_change_invalidates_existing_proposal():
    current_epoch = [7]
    adapters = ProductionAdapters(
        deployment=lambda proposal: {}, epoch=lambda: current_epoch[0],
        approval=lambda proposal: True,
    )
    proposal = adapters.propose_deployment(
        proposal_id="p1", digest=DIGEST, expires_at=fresh_expiry(),
        target="prod", artifact="image",
    )
    current_epoch[0] = 8

    with pytest.raises(ProductionCapabilityError) as exc_info:
        adapters.execute(proposal)

    assert exc_info.value.code == "approval_expired"
    current_epoch[0] = 7
    with pytest.raises(ProductionCapabilityError) as replay:
        adapters.execute(proposal)
    assert replay.value.code == "invalid_proposal"


@pytest.mark.parametrize("approval", [None, lambda proposal: False])
def test_execution_requires_separate_explicit_approval_and_burns_proposal(approval):
    called = []
    adapters = ProductionAdapters(
        deployment=lambda proposal: called.append(proposal) or {}, approval=approval
    )
    proposal = adapters.propose_deployment(
        proposal_id="p1", digest=DIGEST, expires_at=fresh_expiry(),
        target="prod", artifact="image",
    )

    with pytest.raises(ProductionCapabilityError) as exc_info:
        adapters.execute(proposal)
    assert exc_info.value.code == "approval_required"
    assert called == []

    with pytest.raises(ProductionCapabilityError) as replay_error:
        adapters.execute(proposal)
    assert replay_error.value.code == "invalid_proposal"


def test_forged_or_replaced_proposal_is_rejected():
    adapters = ProductionAdapters(deployment=lambda proposal: {})
    original = adapters.propose_deployment(
        proposal_id="p1", digest=DIGEST, expires_at=fresh_expiry(),
        target="prod", artifact="image",
    )
    forged = replace(original, payload={"target": "attacker"})

    with pytest.raises(ProductionCapabilityError) as exc_info:
        adapters.execute(forged)

    assert exc_info.value.code == "invalid_proposal"


def test_provider_result_must_bind_digest_and_evidence():
    for result in (
        {"status": "completed", "provider_evidence": {"id": "run-1"}},
        {"status": "completed", "proposal_digest": "wrong", "provider_evidence": {"id": "run-1"}},
        {"status": "completed", "proposal_digest": "x", "provider_evidence": {}},
    ):
        adapters = ProductionAdapters(
            deployment=lambda proposal, result=result: result,
            approval=lambda proposal: True,
        )
        proposal = adapters.propose_deployment(
            proposal_id="p1", digest=DIGEST, expires_at=fresh_expiry(),
            target="prod", artifact="image",
        )
        with pytest.raises(ProductionCapabilityError) as exc_info:
            adapters.execute(proposal)
        assert exc_info.value.code == "unverified"


@pytest.mark.parametrize('valid_evidence', [False, True])
def test_epoch_change_during_provider_execution_is_in_doubt(valid_evidence):
    current_epoch = [1]

    def callback(proposal):
        current_epoch[0] = 2
        if not valid_evidence:
            return {}
        return {
            "status": "accepted", "proposal_digest": proposal.digest,
            "provider_evidence": {"id": "run-1"},
        }

    adapters = ProductionAdapters(
        deployment=callback, epoch=lambda: current_epoch[0],
        approval=lambda proposal: True,
    )
    proposal = adapters.propose_deployment(
        proposal_id="p1", digest=DIGEST, expires_at=fresh_expiry(),
        target="prod", artifact="image",
    )

    with pytest.raises(ProductionCapabilityError) as exc_info:
        adapters.execute(proposal)

    assert exc_info.value.code == "in_doubt"


def test_expired_attempt_is_consumed_even_if_clock_moves_back(monkeypatch):
    now = [1000]
    monkeypatch.setattr('longflow.production_adapters.time.time', lambda: now[0])
    calls = []
    adapters = ProductionAdapters(deployment=lambda proposal: calls.append(proposal),
                                  approval=lambda proposal: True)
    proposal = adapters.propose_deployment(proposal_id='ignored', digest=DIGEST,
        expires_at=1100, target='prod', artifact='image')
    now[0] = 1100
    with pytest.raises(ProductionCapabilityError) as expired:
        adapters.execute(proposal)
    assert expired.value.code == 'approval_expired'
    now[0] = 1050
    with pytest.raises(ProductionCapabilityError) as replay:
        adapters.execute(proposal)
    assert replay.value.code == 'invalid_proposal'
    assert calls == []


def test_pending_proposal_limit_preserves_existing_and_expiry_releases_capacity(monkeypatch):
    now = [1000]
    monkeypatch.setattr('longflow.production_adapters.time.time', lambda: now[0])
    adapters = ProductionAdapters(deployment=lambda proposal: pytest.fail('Registration must not execute'))
    def propose(expiry):
        return adapters.propose_deployment(proposal_id='ignored', digest=DIGEST,
            expires_at=expiry, target='prod', artifact='image')
    proposals = [propose(1050) for _ in range(256)]
    before = dict(adapters._pending)
    with pytest.raises(ProductionCapabilityError) as error:
        propose(1200)
    assert error.value.code == 'proposal_limit'
    assert adapters._pending == before
    assert all(adapters._pending[p.proposal_id] is p for p in proposals)
    now[0] = 1050
    newest = propose(1250)
    assert adapters._pending == {newest.proposal_id: newest}


def test_registration_prunes_expired_but_preserves_live_proposals(monkeypatch):
    now = [1000]
    monkeypatch.setattr('longflow.production_adapters.time.time', lambda: now[0])
    adapters = ProductionAdapters(deployment=lambda proposal: {})
    def propose(expiry):
        return adapters.propose_deployment(proposal_id='ignored', digest=DIGEST,
            expires_at=expiry, target='prod', artifact='image')
    expired = propose(1050)
    live = propose(1200)
    now[0] = 1050
    newest = propose(1250)
    assert expired.proposal_id not in adapters._pending
    assert adapters._pending[live.proposal_id] is live
    assert adapters._pending[newest.proposal_id] is newest


@pytest.mark.parametrize("status", ["completed", "accepted"])
def test_successful_injected_callback_returns_provider_evidence(status):
    received = []

    def callback(proposal):
        received.append(proposal)
        return {
            "status": status, "proposal_digest": proposal.digest,
            "provider_evidence": {"id": "run-1"},
        }

    adapters = ProductionAdapters(deployment=callback, approval=lambda proposal: True)
    proposal = adapters.propose_deployment(
        proposal_id="p1", digest=DIGEST, expires_at=fresh_expiry(),
        target="prod", artifact="image",
    )

    result = adapters.execute(proposal)

    assert isinstance(proposal, ProductionProposal)
    assert result["status"] == status
    assert result["proposal_digest"] == proposal.digest
    assert received == [proposal]
