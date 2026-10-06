import json
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from casm_monitor.config import Settings
from casm_monitor.store import Store
from casm_monitor.web import calibration_transit as review
from casm_monitor.web import calibration_workspace as workspace


@pytest.fixture
def saved(tmp_path, monkeypatch):
    from casm_vis_analysis import calibration_transit as science
    monkeypatch.setattr(science, 'source_altaz', lambda source, times: (np.full(len(times), -20.), np.zeros(len(times))))
    settings = workspace.workspace_settings(Settings(store_root=tmp_path/'production', observation_cache_root=tmp_path/'preview'))
    root = workspace.build_dir(settings, 'test')
    identity = 'a'*32
    dest = root/'actions'/identity
    dest.mkdir(parents=True)
    action = dict(action='transit', action_id=identity, finished='2026-10-05T20:00:00Z',
        selection=dict(source='cyg-a', date='2026-09-25', off_minutes=90, minimum_snr=5),
        result=dict(schema_version=2, center_unix=0., antennas=[9,10], metrics={'candidate':{}},
                    amplitude_comparison={'antennas':[9,10], 'schemes':{'uniform':{}}}))
    (root/'request.json').write_text(json.dumps({'params':{'static_window':None}}))
    (dest/'result.json').write_text(json.dumps(action))
    times = np.linspace(-150,150,61)*60
    freq = np.linspace(480,390,8)
    signal = np.exp(-.5*(times[:,None]/1200)**2)*np.ones((1,8))
    values = signal+np.random.default_rng(42).normal(0,.01,signal.shape)
    for name in ('transit-candidate', 'amplitude-uniform-transit'):
        np.savez(dest/(name+'.npz'), spectrum=values, time_unix=times, freq_mhz=freq,
                 pair_weight_sum=np.full(8,2.), exact_response=signal)
    return settings, root, dest, identity, action


def test_saved_routes_are_read_only_and_zoom_images_available(saved):
    settings, root, dest, identity, action = saved
    before = {p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    store = Store(settings.db_path, store_root=settings.store_root)
    app = FastAPI()
    app.include_router(workspace.build_router(settings, store))
    url = '/api/calibration/builds/test/transit/'+identity
    with TestClient(app) as client:
        response = client.get(url)
        assert response.status_code == 200, response.text
        info = response.json()
        assert info['background']['valid'] and all(i['available'] for i in info['items'])
        assert info['items'][0]['label'] == 'Without static subtraction'
        for scale in ('sum','mean'):
            for kind in ('transit','timing','calibration','amplitude'):
                response = client.get(url+f'/{kind}.png?series=cal-0&scale={scale}')
                assert response.status_code == 200, response.text
                assert response.content.startswith(b'\x89PNG')
        assert client.get(url+'?scale=invalid').status_code == 400
        assert client.get(url+'/bad.png').status_code == 400
        assert client.get(url+'/transit.png?series=bad').status_code == 400
        assert not store.list_jobs()
    assert before == {p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    store.close()


def test_invalid_background_does_not_reuse_old_metrics(saved, monkeypatch):
    from casm_vis_analysis import calibration_transit as science
    settings, root, dest, identity, action = saved
    monkeypatch.setattr(science, 'source_altaz', lambda source,times:(np.full(len(times),20.),np.zeros(len(times))))
    info = review.transit_review(settings, 'test', identity)
    assert not info['background']['valid']
    assert all(not i['available'] for i in info['items'])
    with pytest.raises(ValueError, match='Sun-down'):
        review.transit_png(settings, 'test', identity, 'transit', 'cal-0')


def test_legacy_normalization_missing_allows_raw_sum_only(saved):
    settings, root, dest, identity, action = saved
    path = dest/'transit-candidate.npz'
    with np.load(path) as data:
        payload = {k:data[k] for k in data.files if k != 'pair_weight_sum'}
    np.savez(path, **payload)
    info = review.transit_review(settings, 'test', identity)
    assert not info['items'][0]['available'] and 'normalization' in info['items'][0]['reason']
    assert review.transit_review(settings, 'test', identity, 'sum')['items'][0]['available']
    with pytest.raises(ValueError, match='Complete matched'):
        review.transit_png(settings, 'test', identity, 'calibration')


def test_legacy_profile_normalizer_matches_antennas_and_reversed_frequencies(saved):
    settings, root, dest, identity, action = saved
    path = dest/'amplitude-uniform-transit.npz'
    with np.load(path) as data:
        payload = {k:data[k] for k in data.files if k != 'pair_weight_sum'}
    np.savez(path, **payload)
    inspection = root/'actions'/('b'*32)
    (inspection/'inspection').mkdir(parents=True)
    (inspection/'result.json').write_text(json.dumps({'action':'inspect','finished':'2026-10-04T00:00:00Z'}))
    profile = inspection/'inspection'/'antennas.npz'
    np.savez(profile, antennas=[10,9,11], freq_mhz=payload['freq_mhz'][::-1],
             amp_uniform=np.array([2.,3.,900.])[:,None]*np.ones((1,8)))
    entry = ('amp-uniform','amplitude','Uniform',str(path),None,str(profile))
    np.testing.assert_allclose(review._denominator(payload, entry, action), 12.)
    assert all(i['available'] for i in review.transit_review(settings, 'test', identity)['items'])


def test_ids_symlinks_and_mismatched_samples_rejected(saved):
    settings, root, dest, identity, action = saved
    with pytest.raises(ValueError): review.transit_review(settings,'test','../bad')
    path = dest/'amplitude-uniform-transit.npz'
    with np.load(path) as data: payload = dict(data)
    payload['time_unix'] = payload['time_unix']+1
    np.savez(path, **payload)
    with pytest.raises(ValueError, match='integration grids'):
        review.transit_review(settings, 'test', identity)
    path.rename(dest/'original.npz')
    path.symlink_to(dest/'original.npz')
    with pytest.raises(ValueError, match='symlinks'):
        review.transit_review(settings, 'test', identity)
