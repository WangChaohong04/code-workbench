"""Retained provider-state contract note; no external provider is contacted."""
from longflow.production_adapters import ProductionAdapters


def test_empty_injected_production_adapter_is_not_wired():
    adapter = ProductionAdapters()
    capabilities = adapter.capabilities()
    assert capabilities['automatic_execution'] is False
    assert capabilities['ci_cd']['status'] == 'not_connected'
    assert capabilities['production_deployment']['status'] == 'not_connected'
    assert capabilities['production_database']['status'] == 'not_connected'
    assert capabilities['production_restore']['status'] == 'not_connected'
    assert adapter.deployment is None
    assert adapter.database is None
