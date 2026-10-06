"""Product-scope regressions: removed features must not remain executable."""
from collections import Counter

from fastapi.testclient import TestClient
from longflow import api, config


def test_only_engineering_routes_and_tools(tmp_path):
    cfg = config.load_config()
    cfg['db_path'] = str(tmp_path / 'scope.db')
    app = api.create_app(cfg)
    paths = {route.path for route in app.routes}
    assert '/api/development/tasks' in paths
    assert '/api/tasks/{task_id}/development/contract' in paths
    for removed in ('/api/route', '/api/domains', '/api/subagents/run',
                    '/api/knowledge/import', '/api/improvements', '/api/eval/run'):
        assert removed not in paths
    assert app.state.lf.registry.names() == ['kb_search', 'record_note']
    keys = [(route.path, method) for route in app.routes
            for method in getattr(route, 'methods', ())]
    assert all(count == 1 for count in Counter(keys).values())
    app.state.lf.conn.close()


def test_legacy_scenario_cannot_reactivate_removed_flow(tmp_path):
    cfg = config.load_config()
    cfg['db_path'] = str(tmp_path / 'legacy.db')
    app = api.create_app(cfg)
    client = TestClient(app)
    response = client.post('/api/tasks', json={'goal': 'legacy request', 'scenario': 'team_ops'})
    assert response.status_code == 422
    assert app.state.lf.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
    app.state.lf.conn.close()
