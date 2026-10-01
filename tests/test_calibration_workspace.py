import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from casm_monitor.config import Settings
from casm_monitor.store import Store
from casm_monitor.jobs.calibration_workspace import notebook_executed
from casm_monitor.web import calibration_workspace as workspace


@pytest.fixture
def setup(tmp_path, monkeypatch):
    layout = tmp_path / 'layout.csv'
    layout.write_text('antenna,x,y,z,functional,include_in_beamforming\n9,0,0,0,1,1\n10,0,10,0,1,0\n11,0,20,0,0,0\n')
    settings = workspace.workspace_settings(Settings(store_root=tmp_path / 'production', observation_cache_root=tmp_path / 'preview', snap_layout_csv=layout))
    store = Store(settings.db_path, store_root=settings.store_root)
    monkeypatch.setattr(workspace, 'deployed_product', lambda settings: {})
    def validate(body, selected):
        assert workspace.layout_info(selected.layout_csv)['antennas'] == [9, 10]
        return dict(source='sun', tag=body['tag'], source_window=body['source_window'], static_window=body.get('static_window'),
                    antennas=body['antennas'], ref_ant=9, prev_cal_path=None)
    monkeypatch.setattr(workspace, 'validate', validate)
    app = FastAPI()
    app.include_router(workspace.build_router(settings, store))
    day = (datetime.now(timezone.utc) - timedelta(days=2)).strftime('%Y-%m-%d')
    body = dict(tag='test_revision', source='sun', reviewed=True, antennas=[9, 10], ref_ant=9,
                source_window=[day + ' 19:00:00', day + ' 20:00:00'])
    with TestClient(app) as client:
        yield settings, store, client, body
    store.close()


def test_create_is_durable_isolated_and_immutable(setup):
    settings, store, client, body = setup
    original = settings.layout_csv.read_bytes()
    response = client.post('/api/calibration/builds', json=body)
    assert response.status_code == 200, response.text
    job = store.get_job(response.json()['job_id'])
    assert job['kind'] == 'calibration_workspace' and job['state'] == 'queued'
    assert job['params']['request_sha256']
    assert settings.layout_csv.read_bytes() == original
    assert client.post('/api/calibration/builds', json=body).status_code == 409
    detail = client.get('/api/calibration/builds/test_revision').json()
    assert detail['request']['params']['antennas'] == [9, 10]
    assert not detail['summary']
    assert client.post('/api/calibration/builds/test_revision/actions', json={'action': 'grid', 'replace_custom_cells': True}).status_code == 400
    assert client.post(f'/api/calibration/jobs/{job["id"]}/cancel', json={}).status_code == 200
    assert store.get_job(job['id'])['state'] == 'cancelled'


def test_four_hour_windows_are_accepted(setup):
    _, _, client, body = setup
    day = body['source_window'][0][:10]
    body.update(source_window=[day + ' 15:00:00', day + ' 19:00:00'],
                static_window=[day + ' 03:00:00', day + ' 07:00:00'])
    response = client.post('/api/calibration/builds', json=body)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize('key', ['source_window', 'static_window'])
def test_windows_over_four_hours_are_rejected(setup, key):
    _, _, client, body = setup
    day = body['source_window'][0][:10]
    body[key] = [day + ' 03:00:00', day + ' 07:00:01']
    response = client.post('/api/calibration/builds', json=body)
    assert response.status_code == 400
    assert 'limited to four hours' in response.json()['detail']


@pytest.mark.parametrize('explicit', [False, True])
def test_four_hour_phase_and_worker_revalidation(setup, explicit):
    _, _, _, body = setup
    day = body['source_window'][0][:10]
    body['source_window'] = [day + ' 15:00:00', day + ' 19:00:00']
    record = {'params': body}
    request = {'action': 'phase'}
    if explicit:
        request['window'] = body['source_window']
    selected = workspace.action_params(request, record)
    assert selected['window'] == body['source_window']
    assert workspace.action_params(selected, record) == selected
    with pytest.raises(ValueError, match='four hours'):
        workspace.action_params({**request, 'window': [day + ' 15:00:00', day + ' 19:00:01']}, record)


def test_history_inherits_solve_duration_and_worker_revalidation(setup):
    _, _, _, body = setup
    day = body['source_window'][0][:10]
    body['source_window'] = [day + ' 15:00:00', day + ' 19:00:00']
    record = {'params': body}
    selected = workspace.action_params({'action': 'history'}, record)
    assert selected['minutes'] == 240
    assert workspace.action_params(selected, record) == selected
    for minutes in (241, 0, float('nan')):
        with pytest.raises(ValueError, match='four hours'):
            workspace.action_params({'action': 'history', 'minutes': minutes}, record)


def test_phase_worker_reads_full_source_and_static_windows(setup, monkeypatch, tmp_path):
    import numpy as np
    import bf_weights_generator
    from types import SimpleNamespace
    from casm_monitor.jobs import calibration_workspace as worker
    from casm_vis_analysis import calibration_checks

    settings, _, _, body = setup
    day = body['source_window'][0][:10]
    body.update(source_window=[day + ' 15:00:00', day + ' 19:00:00'],
                static_window=[day + ' 03:00:00', day + ' 07:00:00'])
    record = {'params': body}
    selection = workspace.action_params({'action': 'phase'}, record)
    calls = []

    def read_selected(data_dir, layout, antennas, window):
        calls.append(window)
        return {'freq_mhz': np.array([500., 400.]), 'vis': np.ones((3, 2, 1))}, None, {}

    monkeypatch.setattr(worker, 'read_json', lambda path: {'paths': {'cal_h5': 'test.h5'}})
    monkeypatch.setattr(bf_weights_generator, 'load_calibration_weights', lambda path:
                        SimpleNamespace(flags=np.zeros(2, dtype=bool), frequencies_hz=np.array([500e6, 400e6])))
    monkeypatch.setattr(calibration_checks, 'read_selected', read_selected)
    monkeypatch.setattr(calibration_checks, 'render_phase', lambda *args: {})
    result = worker.diagnose(settings, tmp_path, tmp_path, record, 'phase', selection)
    assert calls == [body['source_window'], body['static_window']]
    assert result['static_used']


def test_membership_and_review_gate(setup):
    settings, _, client, body = setup
    for change in ({'reviewed': False}, {'antennas': [9, 11]}, {'antennas': [9, 9]}, {'antennas': [True, 10]}):
        assert client.post('/api/calibration/builds', json={**body, **change}).status_code == 400
    with pytest.raises(HTTPException):
        workspace.review_gate(settings, body['tag'])


def test_sky_planner_is_read_only_and_validates_date(setup, monkeypatch):
    from casm_monitor.web import calibration_planning
    _, store, client, _ = setup
    before = store.list_jobs()
    def plan(date):
        if date == 'bad':
            raise ValueError('bad date')
        return {'date': date, 'windows': []}
    monkeypatch.setattr(calibration_planning, 'sky_plan', plan)
    response = client.get('/api/calibration/sky-plan?date=2026-09-18')
    assert response.status_code == 200
    assert response.json()['date'] == '2026-09-18'
    assert client.get('/api/calibration/sky-plan?date=bad').status_code == 400
    assert store.list_jobs() == before


def test_action_admission_and_grid_roundtrip():
    record = {'params': {'antennas': [9, 10, 12], 'ref_ant': 9}}
    for request in ({'action': 'other'}, {'action': 'grid', 'alt_min': float('nan'), 'replace_custom_cells': True}, {'action': 'grid'}):
        with pytest.raises(ValueError):
            workspace.action_params(request, record)
    grid = workspace.action_params({'action': 'grid', 'alt_min': 20, 'replace_custom_cells': True}, record)
    assert workspace.action_params(grid, record) == grid


def test_isolation_refuses_nested_store_and_symlinks(tmp_path):
    settings = Settings(store_root=tmp_path / 'prod', observation_cache_root=tmp_path / 'prod' / 'child')
    with pytest.raises(ValueError):
        workspace.workspace_settings(settings)
    root = tmp_path / 'preview'
    root.mkdir()
    (root / 'calibration').symlink_to(tmp_path / 'prod')
    with pytest.raises(ValueError):
        workspace.workspace_settings(dataclasses.replace(settings, observation_cache_root=root))


def test_workspace_cannot_inherit_live_upload_authority(tmp_path):
    settings = Settings(store_root=tmp_path / 'production', observation_cache_root=tmp_path / 'preview', allow_upload=True)
    assert not workspace.workspace_settings(settings).allow_upload


def test_artifacts_reject_escaping_symlinks(setup, tmp_path):
    settings, _, client, body = setup
    assert client.post('/api/calibration/builds', json=body).status_code == 200
    root = workspace.build_dir(settings, body['tag'])
    secret = tmp_path / 'secret.json'
    secret.write_text('private')
    (root / 'escape.json').symlink_to(secret)
    assert 'escape.json' not in [item['name'] for item in workspace.artifact_entries(settings, body['tag'])]


def test_notebook_must_really_execute(tmp_path):
    path = tmp_path / 'notebook.ipynb'
    assert not notebook_executed(None)
    for outputs, count, expected in [([], None, False), ([{'output_type': 'error'}], 1, False),
                                      ([{'text': 'MISSING FIGURE: example'}], 1, False), ([], 1, True)]:
        path.write_text(json.dumps({'cells': [{'cell_type': 'code', 'execution_count': count, 'outputs': outputs}]}))
        assert notebook_executed(path) == expected
