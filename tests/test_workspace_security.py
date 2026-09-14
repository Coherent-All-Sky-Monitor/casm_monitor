from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from casm_monitor.web.app import create_app


def test_workspace_requires_readonly_and_isolated_root(settings, store, monkeypatch, tmp_path):
    monkeypatch.setenv('CASM_MONITOR_WORKSPACE','1')
    monkeypatch.delenv('CASM_MONITOR_READ_ONLY',raising=False)
    with pytest.raises(ValueError,match='read-only'):
        create_app(settings)
    with pytest.raises(ValueError,match='outside'):
        create_app(replace(settings,observation_cache_root=settings.store_root/'views'),read_only=True)


@pytest.mark.parametrize('read_only',[True,False])
def test_workspace_disabled_refuses_new_mutations(settings,store,tmp_path,monkeypatch,read_only):
    monkeypatch.delenv('CASM_MONITOR_WORKSPACE',raising=False)
    settings=replace(settings,observation_cache_root=tmp_path/'preview')
    with TestClient(create_app(settings,read_only=read_only)) as client:
        for route in ['/api/commissioning/stage','/api/science/render','/api/review','/api/snap-workspace/render']:
            response=client.post(route,json={},headers={'Origin':'http://testserver','X-CASM-Workspace':'1'})
            assert response.status_code==403


def test_workspace_origin_guard_and_operational_denial(settings,store,tmp_path,monkeypatch):
    monkeypatch.setenv('CASM_MONITOR_WORKSPACE','1')
    settings=replace(settings,observation_cache_root=tmp_path/'preview',t2_db=tmp_path/'absent.sqlite')
    with TestClient(create_app(settings,read_only=True)) as client:
        assert client.post('/api/science/render',json={}).status_code==403
        assert client.post('/api/science/render',json={},headers={'Origin':'https://evil.example','X-CASM-Workspace':'1'}).status_code==403
        headers={'Origin':'http://testserver','X-CASM-Workspace':'1'}
        assert client.post('/api/science/render',json={},headers=headers).status_code==422
        for route in ['/api/jobs','/api/snaps/board-read','/api/cal/build','/api/cal/upload','/api/injections']:
            assert client.post(route,json={},headers=headers).status_code==403
        queue=client.get('/api/review').json()
        headers['X-CASM-Review-CSRF']=queue['csrf_token']
        item=client.post('/api/review',json={'title':'Test evidence','note':'Not a live investigation','selection':{'test':True}},headers=headers)
        assert item.status_code==201
        assert item.json()['state']=='queued'
        requested=client.post('/api/review/'+item.json()['id']+'/request',json={},headers=headers)
        assert requested.status_code==200 and requested.json()['state']=='requested'
        assert (tmp_path/'preview'/'investigations.sqlite').is_file()
