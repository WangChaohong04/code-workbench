"""Retained mock tests; never contact a production provider."""
import time
import pytest
from longflow.production_adapters import ProductionAdapters, ProductionCapabilityError


def test_provider_transport_failure_is_in_doubt_and_not_replayable():
    calls = []

    def interrupted(proposal):
        calls.append(proposal.proposal_id)
        raise TimeoutError('provider may have accepted the request')

    adapters = ProductionAdapters(deployment=interrupted, approval=lambda proposal: True)
    proposal = adapters.propose_deployment(
        proposal_id='ignored', digest='a' * 64, expires_at=int(time.time()) + 120,
        target='disposable-test', artifact='reviewed-image',
    )
    with pytest.raises(ProductionCapabilityError) as error:
        adapters.execute(proposal)
    assert error.value.code == 'in_doubt'
    with pytest.raises(ProductionCapabilityError) as replay:
        adapters.execute(proposal)
    assert replay.value.code == 'invalid_proposal'
    assert calls == [proposal.proposal_id]


@pytest.mark.parametrize('field', ['deployment', 'database', 'epoch', 'approval'])
def test_noncallable_provider_configuration_is_rejected(field):
    with pytest.raises(ProductionCapabilityError) as error:
        ProductionAdapters(**{field: 'untrusted-config-value'})
    assert error.value.code == 'invalid_configuration'
