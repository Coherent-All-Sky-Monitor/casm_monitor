from dataclasses import replace
from datetime import datetime, timedelta, timezone

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
        for route in ['/api/commissioning/stage','/api/science/render','/api/review','/api/snap-workspace/render',
                      '/api/calibration/builds/test/subtraction']:
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
        assert client.post('/api/calibration/builds', json={}).status_code == 403
        assert client.post('/api/calibration/builds', json={}, headers={'Origin': 'https://evil.example', 'X-CASM-Workspace': '1'}).status_code == 403
        assert client.post('/api/calibration/builds', json={}, headers=headers).status_code == 400
        assert client.post('/api/calibration/deployment/build', json={}, headers=headers).status_code == 403
        assert client.post('/api/calibration/deployment/builds/test/upload', json={}, headers=headers).status_code == 403
        queue=client.get('/api/review').json()
        headers['X-CASM-Review-CSRF']=queue['csrf_token']
        item=client.post('/api/review',json={'title':'Test evidence','note':'Not a live investigation','selection':{'test':True}},headers=headers)
        assert item.status_code==201
        assert item.json()['state']=='queued'
        requested=client.post('/api/review/'+item.json()['id']+'/request',json={},headers=headers)
        assert requested.status_code==200 and requested.json()['state']=='requested'
        assert (tmp_path/'preview'/'investigations.sqlite').is_file()


@pytest.mark.parametrize('initial_static', [True, False])
def test_subtraction_prepare_through_full_app(settings, store, tmp_path, monkeypatch, initial_static):
    """Exercise the real middleware and queue; no worker or science solve runs."""
    from casm_monitor.store import Store
    from casm_monitor.web import calibration_workspace as workspace

    monkeypatch.setenv('CASM_MONITOR_WORKSPACE', '1')
    layout = tmp_path / 'layout.csv'
    layout.write_text('antenna,x,y,z,functional,include_in_beamforming\n9,0,0,0,1,1\n10,0,10,0,1,1\n')
    settings = replace(settings, observation_cache_root=tmp_path/'preview',
                       snap_layout_csv=layout, t2_db=tmp_path/'absent.sqlite')
    monkeypatch.setattr(workspace, 'deployed_product', lambda settings: {})
    monkeypatch.setattr(workspace, 'validate', lambda body, selected: {
        key: body[key] for key in ('tag', 'source', 'source_window', 'static_window', 'antennas', 'ref_ant')})
    day = (datetime.now(timezone.utc) - timedelta(days=2)).strftime('%Y-%m-%d')
    night = [day+' 03:00:00', day+' 03:30:00']
    body = dict(tag='subtraction_security', source='sun', reviewed=True, antennas=[9, 10], ref_ant=9,
                source_window=[day+' 19:00:00', day+' 20:00:00'],
                static_window=night if initial_static else None, night_window=night)
    headers = {'Origin': 'http://testserver', 'X-CASM-Workspace': '1'}
    route = '/api/calibration/builds/subtraction_security/subtraction'
    choice = {'static': not initial_static, 'reviewed': True}
    with TestClient(create_app(settings, read_only=True)) as client:
        created = client.post('/api/calibration/builds', json=body, headers=headers)
        assert created.status_code == 200, created.text
        # Cancel the fixture's queued solve; only the request/queue flow is tested.
        assert client.post(f'/api/calibration/jobs/{created.json()["job_id"]}/cancel',
                           json={}, headers=headers).status_code == 200
        isolated = workspace.workspace_settings(settings)
        original = (isolated.cal_builds_root/body['tag']/'request.json').read_bytes()
        for unsafe in ({}, {'Origin': 'http://testserver'}, {'X-CASM-Workspace': '1'},
                       {**headers, 'Origin': 'https://evil.example'},
                       {**headers, 'Origin': 'http://testserver:8062'}):
            assert client.post(route, json=choice, headers=unsafe).status_code == 403
        assert client.put(route, json=choice, headers=headers).status_code == 403
        unreviewed = client.post(route, json={**choice, 'reviewed': False}, headers=headers)
        assert unreviewed.status_code == 400, unreviewed.text
        assert 'Confirm the shared recipe' in unreviewed.json()['detail']
        prepared = client.post(route, json=choice, headers=headers)
        assert prepared.status_code == 200, prepared.text
        child = prepared.json()
        detail = client.get(f'/api/calibration/builds/{child["tag"]}').json()
        assert detail['request']['params']['static_window'] == (None if initial_static else night)
        assert detail['request']['night_window'] == night
        assert (isolated.cal_builds_root/body['tag']/'request.json').read_bytes() == original
        with Store(isolated.db_path, store_root=isolated.store_root, read_only=True) as queue:
            job = queue.get_job(child['job_id'])
            assert job['kind'] == 'calibration_workspace' and job['state'] == 'queued'
        assert store.list_jobs() == []  # Production fixture store stays untouched.
        for forbidden in ('/api/calibration/deployment/builds/test/upload', '/api/cal/upload', '/api/snaps/board-read'):
            assert client.post(forbidden, json={}, headers=headers).status_code == 403
