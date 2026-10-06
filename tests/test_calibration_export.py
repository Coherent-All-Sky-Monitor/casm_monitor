"""Entire release flow uses tiny fixture products and a fake deploy executable."""
from dataclasses import replace
import json
from pathlib import Path
import uuid

import h5py
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from casm_monitor.cal_defaults import sha256_file
from casm_monitor.jobs import calibration_export as export
from casm_monitor.jobs import deploy
from casm_monitor.store import Store
from casm_monitor.web import calibration_workspace as workspace
from casm_monitor.web.cal import build_router, CSRF_COOKIE, CSRF_HEADER
from casm_monitor.web.app import create_app
from test_cal import cal_settings, make_build, fake_deploy_runner, authorize, ANTENNAS


@pytest.fixture
def product(cal_settings, tmp_path, monkeypatch):
    settings = replace(cal_settings, observation_cache_root=tmp_path/'preview',
                       cal_export_roots=(tmp_path/'exports',), allow_upload=True,
                       cal_workspace_allow_upload=True)
    settings = workspace.workspace_settings(settings)
    monkeypatch.setattr(deploy, 'load_settings', lambda _p=None: settings)
    monkeypatch.setattr(deploy, 'casm_track_processes', lambda: [])
    summary = make_build(settings, kind='calibration_workspace', n_beams=512, notebook_executed=True)
    tag = summary['tag']
    root = settings.cal_builds_root/tag
    # Sparse HDF5: real 512-beam shape and populated check channel; no real weights.
    with h5py.File(summary['paths']['weights_h5'], 'r+') as handle:
        del handle['weights_int8']
        data = handle.create_dataset('weights_int8', shape=(2,1501,1,512,64), dtype='i1', chunks=(2,1,1,512,64), compression='gzip')
        for ant in ANTENNAS:
            data[:,1500,:,:,ant-1] = 100
        handle.attrs['n_beams'] = 512
        for axis in ('alt_deg', 'az_deg'):
            del handle['pointings/'+axis]
            handle.create_dataset('pointings/'+axis, shape=(512,))
    for key, name in [('cal_h5','cal.h5'),('report_json','report.json'),('notebook','review.ipynb')]:
        path = root/name
        path.write_text('{}')
        summary['paths'][key] = str(path)
    record = dict(params={}, imported=False)
    workspace.save_json(root/'request.json', record)
    (root/'selected_layout.csv').write_bytes((root/'layout_snapshot.csv').read_bytes())
    workspace.save_json(root/'summary.json', summary)
    stage = root/'stage'
    stage.mkdir()
    runner = fake_deploy_runner(stage)
    monkeypatch.setattr(deploy, '_run', runner)
    monkeypatch.setattr(deploy, 'ensure_registered', lambda *args: {'product_id':'test-only', 'registered_here':True})
    return settings, tag, root, record, runner


def write_export(product):
    settings, tag, root, record, _ = product
    dest = root/'actions'/uuid.uuid4().hex
    dest.mkdir(parents=True)
    item = export.export_product(settings, root, dest, record, {
        'parent':str(settings.cal_export_roots[0]), 'summary_sha256':sha256_file(root/'summary.json')})
    workspace.save_json(dest/'result.json', dict(action='export', result=item))
    return item


def stage_export(product):
    settings, tag, *_ = product
    item = write_export(product)
    deploy.run_stage({'build_tag':tag, 'export_id':item['export_id'], 'export_manifest_sha256':item['manifest_sha256']})
    return item, deploy.load_stage(settings, tag)


def approve(product, item, stage):
    settings, tag, root, *_ = product
    review = dict(accepted=True, note='Fixture-only approval', summary_sha256=sha256_file(root/'summary.json'),
                  stage_digest=deploy.stage_digest(stage), export_id=item['export_id'],
                  export_manifest_sha256=item['manifest_sha256'])
    workspace.save_json(root/'review.json', review)
    return review


def test_export_is_identified_complete_and_bit_identical(product):
    settings, tag, root, _, runner = product
    before = {p:sha256_file(p) for p in root.iterdir() if p.is_file()}
    first, second = write_export(product), write_export(product)
    assert first['directory'] != second['directory']
    assert Path(first['directory']).parent.name == '20260908'
    assert tag in Path(first['directory']).name and first['export_id'] in first['directory']
    assert not first['uploaded'] and first['state'] == 'written'
    assert export.verify_export(settings, tag, first['export_id']) == first
    assert export.verify_export(settings, tag, second['export_id']) == second
    assert {'CB','IB','cal_h5','notebook','request.json','summary.json','report_json'} <= {f['role'] for f in first['files']}
    for file in first['files']:
        assert sha256_file(Path(first['directory'])/file['relative']) == file['sha256'] == sha256_file(file['source'])
    assert before == {p:sha256_file(p) for p in before}
    assert not runner.calls


def test_durable_export_action_publishes_a_verifiable_receipt(product, monkeypatch):
    from casm_monitor.jobs import calibration_workspace as worker
    settings, tag, root, record, runner = product
    summary=json.loads((root/'summary.json').read_text())
    record.update(layout=summary['layout'], selected_layout_sha256=sha256_file(root/'selected_layout.csv'))
    workspace.save_json(root/'request.json',record)
    monkeypatch.setattr(worker,'load_settings',lambda:settings)
    identity=uuid.uuid4().hex
    result=worker.run(dict(tag=tag,action='export',action_id=identity,
        request_sha256=sha256_file(root/'request.json'),selection=dict(action='export',
        parent=str(settings.cal_export_roots[0]),summary_sha256=sha256_file(root/'summary.json'))))
    assert result['action_id']==identity
    assert 'casm_monitor.jobs.calibration_export' in result['code']
    assert export.verify_export(settings,tag,identity)==result['result']
    assert not runner.calls


@pytest.mark.parametrize('which', ['copy','original','manifest','summary'])
def test_tampered_export_refuses_stage_before_executable(product, which):
    settings, tag, root, _, runner = product
    item = write_export(product)
    file = next(f for f in item['files'] if f['role']=='CB')
    path = {'copy':Path(item['directory'])/file['relative'], 'original':Path(file['source']),
            'manifest':Path(item['directory'])/'manifest.json', 'summary':root/'summary.json'}[which]
    # Whitespace changes hashes without corrupting JSON parsing.
    with path.open('ab') as output:
        output.write(b' ')
    with pytest.raises((ValueError, deploy.DeployError)):
        deploy.run_stage({'build_tag':tag, 'export_id':item['export_id'], 'export_manifest_sha256':item['manifest_sha256']})
    assert not runner.calls


def test_export_paths_and_missing_pair_are_rejected(product, tmp_path):
    settings, tag, root, record, runner = product
    for path in ('relative', '/', str(tmp_path/'not-allowed'), str(settings.cal_export_roots[0]/'..'/'escape')):
        with pytest.raises(ValueError):
            export.export_parent(settings, path)
    link = settings.cal_export_roots[0]
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        export.export_parent(settings, link/'nested')
    link.unlink()
    summary = json.loads((root/'summary.json').read_text())
    Path(summary['paths']['ib_h5']).unlink()
    with pytest.raises((ValueError, deploy.DeployError)):
        write_export(product)
    assert not link.exists() and not runner.calls


def test_failed_copy_never_publishes_completed_directory(product, monkeypatch):
    settings, _, _, _, runner = product
    def fail(*args, **kwargs):
        raise OSError('fixture disk failure')
    monkeypatch.setattr(export.shutil, 'copyfileobj', fail)
    with pytest.raises(OSError, match='disk failure'):
        write_export(product)
    folders = list(settings.cal_export_roots[0].glob('*/*'))
    assert len(folders)==1 and folders[0].name.endswith('.incomplete')
    assert not list(settings.cal_export_roots[0].rglob('manifest.json'))
    assert not runner.calls


def test_review_required_bound_to_exact_export_and_dry_run(product):
    settings, tag, root, _, runner = product
    item, stage = stage_export(product)
    assert len(stage['md5s']) == 12 and stage['checks_ok']
    assert '--no-registry' in runner.calls[0]
    assert not {'--upload','--save-defaults','--dry-run'} & set(runner.calls[0])
    with pytest.raises(deploy.DeployError, match='human review'):
        deploy.upload_plan(settings, tag, tag)
    approve(product, item, stage)
    assert export.verify_review(settings, tag)['export_id'] == item['export_id']
    deploy.upload_plan(settings, tag, tag)
    with pytest.raises(deploy.DeployError, match='locked'):
        deploy.upload_plan(replace(settings, cal_workspace_allow_upload=False), tag, tag)
    old = stage['export_id']
    stage['export_id'] = uuid.uuid4().hex
    with pytest.raises(ValueError, match='changed since review'):
        export.verify_review(settings, tag, stage)
    stage['export_id'] = old
    (root/'review.json').write_text('{}')
    with pytest.raises(ValueError, match='human review'):
        export.verify_review(settings, tag, stage)


def test_mock_upload_requires_single_use_auth_and_rechecks_review(product):
    settings, tag, root, _, runner = product
    item, stage = stage_export(product)
    approve(product, item, stage)
    auth = authorize(settings, tag)
    result = deploy.run_upload({'authorization_id':auth, 'build_tag':tag})
    assert result['exit_code']==0
    assert '--upload' in runner.calls[-1] and '--save-defaults' not in runner.calls[-1]
    with pytest.raises(deploy.DeployError):
        deploy.run_upload({'authorization_id':auth, 'build_tag':tag})
    auth = authorize(settings, tag)
    (root/'review.json').write_text('{}')
    count = len(runner.calls)
    with pytest.raises(deploy.DeployError, match='human review'):
        deploy.run_upload({'authorization_id':auth, 'build_tag':tag})
    assert len(runner.calls)==count


def test_web_export_stage_review_and_upload_queue(product):
    settings, tag, root, _, runner = product
    with Store(settings.db_path, store_root=settings.store_root) as store:
        app = FastAPI()
        app.include_router(workspace.build_router(settings, store))
        app.include_router(build_router(store, store, settings, prefix='/api/calibration/deployment',
                           review_gate=workspace.review_gate, stage_gate=workspace.export_stage_gate))
        with TestClient(app) as client:
            base = f'/api/calibration/builds/{tag}'
            route = f'/api/calibration/deployment/builds/{tag}'
            response = client.post(base+'/export', json={'parent':str(settings.cal_export_roots[0])})
            assert response.status_code==200, response.text
            job = store.get_job(response.json()['job_id'])
            assert job['kind']=='calibration_workspace' and job['params']['action']=='export'
            assert job['params']['selection']['summary_sha256']==sha256_file(root/'summary.json')
            store.cancel_job(job['id'])
            assert client.post(route+'/stage', json={}).status_code==409
            item, stage = stage_export(product)
            response = client.post(route+'/stage', json={'export_id':item['export_id']})
            assert response.status_code==200, response.text
            queued = store.get_job(response.json()['job_id'])
            assert queued['params']['export_manifest_sha256']==item['manifest_sha256']
            store.cancel_job(queued['id'])
            review = dict(accepted=True, note='test', export_id=item['export_id'], stage_digest=deploy.stage_digest(stage))
            assert client.post(base+'/review', json=review).status_code==409  # independent transit missing
            transit = root/'actions'/uuid.uuid4().hex
            transit.mkdir()
            workspace.save_json(transit/'result.json', dict(action='transit', selection={'source':'cyg-a'}))
            assert client.post(base+'/review', json={**review, 'stage_digest':'stale'}).status_code==409
            assert client.post(base+'/review', json=review).status_code==200
            assert client.post(route+'/upload', json={'confirm_tag':tag}).status_code==403
            client.get('/api/calibration/deployment/status')
            headers = {CSRF_HEADER:client.cookies[CSRF_COOKIE]}
            assert client.post(route+'/upload', headers=headers, json={'confirm_tag':'wrong'}).status_code==400
            response = client.post(route+'/upload', headers=headers, json={'confirm_tag':tag})
            assert response.status_code==200, response.text
            job = store.get_job(response.json()['job_id'])
            assert set(job['params'])=={'authorization_id','build_tag'}
            assert not any('--upload' in call for call in runner.calls)  # queue only; no worker


@pytest.mark.parametrize('general,workspace_gate', [(False,False),(True,False),(False,True),(True,True)])
def test_full_app_both_server_gates_and_same_origin(settings, store, tmp_path, monkeypatch, general, workspace_gate):
    monkeypatch.setenv('CASM_MONITOR_WORKSPACE','1')
    settings = replace(settings, observation_cache_root=tmp_path/'preview', t2_db=tmp_path/'missing.sqlite',
                       allow_upload=general, cal_workspace_allow_upload=workspace_gate)
    isolated = workspace.workspace_settings(settings)
    assert isolated.allow_upload == (general and workspace_gate)
    with TestClient(create_app(settings, read_only=True)) as client:
        route='/api/calibration/deployment/builds/fixture/upload'
        headers={'Origin':'http://testserver','X-CASM-Workspace':'1'}
        for unsafe in ({}, {**headers, 'Origin':'https://evil.example'}):
            assert client.post(route,json={},headers=unsafe).status_code==403
        response=client.post(route,json={},headers=headers)
        assert response.status_code==403
        assert ('CSRF' in response.text)==(general and workspace_gate)
        assert client.post('/api/calibration/builds/fixture/export',json={},headers={}).status_code==403
        assert client.post('/api/calibration/builds/fixture/export',json={},headers=headers).status_code==400
    assert store.list_jobs()==[]
