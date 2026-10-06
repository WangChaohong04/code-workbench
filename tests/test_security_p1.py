"""Security boundaries retained for engineering API."""
import pytest
from fastapi.testclient import TestClient
from longflow import api as api_mod, config as cfg_mod, netguard, permissions as perm, db

@pytest.mark.parametrize('url',['file:///etc/passwd','ftp://example.com/x','http://127.0.0.1/','http://169.254.169.254/latest/meta-data/','http://[::1]/'])
def test_ssrf_addresses_blocked(url):
    with pytest.raises(netguard.UrlNotAllowed): netguard.check_url(url, resolve=False)
def test_allowlist_enforced():
    netguard.check_url('https://gov.cn/policy',allowlist=['gov.cn'],resolve=False)
    with pytest.raises(netguard.UrlNotAllowed): netguard.check_url('https://evil.com/x',allowlist=['gov.cn'],resolve=False)
def test_http_get_tool_blocks_non_whitelist(workdir):
    from tests.conftest import tmp_db
    s=tmp_db(workdir)
    assert 'http_get' not in s.tool_names()
    with pytest.raises(netguard.UrlNotAllowed): netguard.check_url('http://169.254.169.254/latest/meta-data/',resolve=False)
def _auth_client(workdir,tokens):
    cfg=cfg_mod.load_config(); cfg['db_path']=str(workdir/'sec.db'); cfg['auth']={'tokens':tokens}; app=api_mod.create_app(cfg); return app,TestClient(app)
TOK={'tok-a':{'user':'alice','role':'member','workspaces':['ws-a'],'default_workspace':'ws-a'},'tok-b':{'user':'bob','role':'member','workspaces':['ws-b'],'default_workspace':'ws-b'},'tok-admin':{'user':'root','role':'admin','workspaces':['*'],'default_workspace':'default'}}
def test_unauthenticated_rejected_in_token_mode(workdir):
    _,c=_auth_client(workdir,TOK); assert c.get('/api/tasks').status_code in (401,403)
def test_body_identity_forgery_rejected(workdir):
    _,c=_auth_client(workdir,TOK); ha={'Authorization':'Bearer tok-a'}
    r=c.post('/api/tasks',headers=ha,json={'goal':'工程任务安全审查','repository':'demo-repo','branch':'main','workspace':'ws-b','owner':'bob'})
    assert r.status_code == 422
def test_tasks_isolated_between_workspaces(workdir):
    _,c=_auth_client(workdir,TOK); ha={'Authorization':'Bearer tok-a'}; hb={'Authorization':'Bearer tok-b'}
    r=c.post('/api/tasks',headers=ha,json={'goal':'工程任务安全审查','repository':'demo-repo','branch':'main'}); assert r.status_code==200; tid=r.json()['task']['id']
    assert any(t['id']==tid for t in c.get('/api/tasks',headers=ha).json()['tasks']); assert not any(t['id']==tid for t in c.get('/api/tasks',headers=hb).json()['tasks']); assert c.get(f'/api/tasks/{tid}',headers=hb).status_code==404
def test_approval_cross_workspace_forbidden(workdir):
    _,c=_auth_client(workdir,TOK); ha={'Authorization':'Bearer tok-a'}; hb={'Authorization':'Bearer tok-b'}; tid=c.post('/api/tasks',headers=ha,json={'goal':'工程审查','repository':'demo-repo','branch':'main'}).json()['task']['id']; assert c.post(f'/api/tasks/{tid}/cancel',headers=hb).status_code in (403,404)
def test_knowledge_docs_isolated(workdir):
    _,c=_auth_client(workdir,TOK); ha={'Authorization':'Bearer tok-a'}; hb={'Authorization':'Bearer tok-b'}; r=c.post('/api/tasks',headers=ha,json={'goal':'private engineering task','repository':'demo-repo','branch':'main'}); assert r.status_code==200; tid=r.json()['task']['id']; assert c.get(f'/api/tasks/{tid}',headers=hb).status_code==404
