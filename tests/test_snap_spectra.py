"""Stored 4096-channel views: no hardware, Kafka, writes or gap fabrication."""
import json

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from casm_monitor.config import Settings
from casm_monitor.store import ShardWriter, Store
from casm_monitor.web import snap_spectra as module

IP = '192.168.120.52'
NOW = 1800000000.0


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    layout = tmp_path / 'layout.csv'
    layout.write_text('antenna,snap,adc,packet_idx,functional,row,col,include_in_beamforming\n'
                      '19,0,6,6,1,N11,E2,1\n')
    mapping = tmp_path / 'map.csv'
    mapping.write_text(f'chassis,slot,feng_id,snap_ip\n1,A,0,{IP}\n')
    settings = Settings(store_root=tmp_path/'store', snap_map_csv=mapping, snap_layout_csv=layout,
                        registry_dir=tmp_path/'registry', observation_cache_root=tmp_path/'preview')
    store = Store(settings.db_path, store_root=settings.store_root)
    shards = ShardWriter(store)
    monkeypatch.setattr(module.time, 'time', lambda: NOW)
    monkeypatch.setattr(module, 'latest_reads', lambda _: {})
    monkeypatch.setattr(module, 'read_latest_vis', lambda _: None)
    readonly = Store(settings.db_path, store_root=settings.store_root, read_only=True)
    app = FastAPI()
    app.include_router(module.build_router(settings, readonly))
    with TestClient(app) as client:
        yield client, store, shards, settings
    readonly.close()
    store.close()


def test_beamforming_unknown_never_uses_layout_intent(fixture):
    client, _, _, _ = fixture
    result = client.get('/api/snap-workspace/beamforming').json()
    assert result['status'] == 'unknown'
    assert all(i['beamforming'] is None for i in result['inputs'])


def test_beamforming_cached_payload_identity_and_wiring(fixture, monkeypatch):
    import h5py
    from casm_monitor.observation import file_identity
    client, store, _, settings = fixture
    product = settings.observation_cache_root / 'payload.h5'
    product.parent.mkdir()
    product.write_bytes(b'Only a stat is needed by GET')
    products = settings.registry_dir / 'products'
    products.mkdir(parents=True)
    (products/'test.json').write_text(json.dumps({'h5_path':str(product)}))
    (settings.registry_dir/'live_events.jsonl').write_text('\n'.join(
        json.dumps({'stream':s,'product_id':'test','utc':'2026-09-25T00:00:00Z'}) for s in range(6)))
    store.put_scalar('weights.product_id', 'test')
    store.put_scalar('weights.product_source', 'live_event')
    cache = dict(product_id='test', path=str(product), identity=file_identity(product),
                 inspection_state='complete', antennas=[19], beams=[dict(beam=0,antennas=[19])],
                 positions=[dict(antenna=19,slot=6)])
    target = settings.observation_cache_root/'membership.json'
    target.write_text(json.dumps(cache))
    def forbidden(*_, **__):
        raise AssertionError('GET must not open weights payload')
    monkeypatch.setattr(h5py, 'File', forbidden)
    result = client.get('/api/snap-workspace/beamforming').json()
    assert result['status'] == 'complete'
    assert [i['adc'] for i in result['inputs'] if i['beamforming']] == [6]
    cache['positions'][0]['antenna'] = 99  # Rewired slot cannot borrow old membership.
    target.write_text(json.dumps(cache))
    result = client.get('/api/snap-workspace/beamforming').json()
    assert result['status'] == 'unknown' and result['unresolved_slots'] == [6]
    assert result['inputs'][6]['beamforming'] is None
    product.write_bytes(b'Changed payload invalidates cache')
    result = client.get('/api/snap-workspace/beamforming').json()
    assert result['status'] == 'unknown'
    assert all(i['beamforming'] is None for i in result['inputs'])


def save(shards, ts=NOW-100, values=None, epoch='e1', ip=IP):
    if values is None:
        values = np.full((12,4096), 1e-8, dtype='float32')
    return shards.write('snap_read', values, t0=ts, meta={'ip':ip,'eq_epoch':epoch,'fft_shift':3})


def test_native_shape_frequency_and_physical_mapping(fixture):
    client, store, shards, _ = fixture
    data = np.full((12,4096), 1e-8, dtype='float32')
    data[6,2048] = 1e-2
    save(shards, values=data)
    result = client.get('/api/snap-workspace/spectra').json()
    assert result['channels'] == 4096
    assert len(result['freq_mhz']) == 4096
    assert result['freq_mhz'][0] == 500
    assert result['freq_mhz'][-1] == pytest.approx(375.030517578125)
    assert result['freq_mhz'][512] == 484.375
    assert result['freq_mhz'][3583] == pytest.approx(390.655517578125)
    assert np.allclose(np.diff(result['freq_mhz']), -125/4096, atol=1e-6, rtol=0)
    board = result['boards'][0]
    assert len(board['inputs']) == len(board['spectra_db']) == 12
    assert board['inputs'][6]['station'] == 'N11E2'
    assert board['inputs'][6]['antenna'] == 19
    assert board['inputs'][6]['packet_idx'] == 6
    assert board['spectra_db'][6][2048] == pytest.approx(-20)
    assert board['spectra_db'][6][0] == pytest.approx(-80)
    assert board['stale'] is False
    assert store.query('SELECT COUNT(*) n FROM jobs')[0]['n'] == 0
    assert store.query('SELECT COUNT(*) n FROM watermarks')[0]['n'] == 0


def test_zero_invalid_not_floored_or_connected(fixture):
    client, _, shards, _ = fixture
    a = np.ones((12,4096), dtype='float32')
    a[0,:4] = [0, np.nan, -1, np.inf]
    save(shards, values=a)
    out = client.get('/api/snap-workspace/spectra').json()['boards'][0]
    assert out['spectra_db'][0][:5] == [None,None,None,None,0]
    assert out['zero_channels'][0] == 1
    assert out['invalid_channels'][0] == 3


def test_history_does_not_hold_across_missing_hours(fixture):
    client, _, shards, _ = fixture
    save(shards, NOW-20000)
    latest = client.get('/api/snap-workspace/spectra').json()['boards'][0]
    assert latest['stale'] is True and latest['ts'] == NOW-20000
    historical = client.get('/api/snap-workspace/spectra', params={'at':NOW-100}).json()['boards'][0]
    assert historical['ts'] is None and historical['spectra_db'] is None
    assert client.get('/api/snap-workspace/spectra', params={'at':NOW-19900}).json()['boards'][0]['ts'] == NOW-20000


def test_trend_averages_linear_power_including_zero_and_marks_missing(fixture):
    client, _, shards, _ = fixture
    a = np.ones((12,4096), dtype='float32')
    a[0,:2048] = 0
    save(shards, NOW-20000, a)
    a[0] = 100
    save(shards, NOW-10000, a, epoch='e2')
    a[0,0] = np.nan
    save(shards, NOW-100, a, epoch='e2')
    result = client.get('/api/snap-workspace/spectra-trend', params={'ip':IP,'adc':0}).json()
    assert [p['ts'] for p in result['points']] == [NOW-20000,NOW-10000,NOW-100]
    assert result['points'][0]['power_db'] == pytest.approx(10*np.log10(.5),abs=1e-4)
    assert result['points'][0]['zero_channels'] == 2048
    assert result['points'][1]['power_db'] == 20
    assert result['points'][1]['eq_epoch'] == 'e2'
    assert result['points'][2]['power_db'] is None
    assert result['gap_s'] == 5400


def test_catalog_preserves_real_timestamps_and_excludes_kafka(fixture):
    client, _, shards, _ = fixture
    save(shards, NOW-20000)
    save(shards, NOW-100)
    shards.write('kafka_bp_full', np.ones((1,1,3072)), t0=NOW-50)
    result = client.get('/api/snap-workspace/spectra-catalog', params={'days':1}).json()
    assert [s['at'] for s in result['snapshots']] == [NOW-20000,NOW-100]
    assert result['reads'] == 2


def test_empty_and_invalid_requests(fixture):
    client, _, _, _ = fixture
    assert client.get('/api/snap-workspace/spectra').json()['boards'][0]['spectra_db'] is None
    assert client.get('/api/snap-workspace/spectra?at=nan').status_code == 400
    for query in ['days=0','days=366','days=nan']:
        assert client.get('/api/snap-workspace/spectra-catalog?'+query).status_code == 422
    assert client.get('/api/snap-workspace/spectra-trend?ip=unknown&adc=0').status_code == 404
    assert client.get(f'/api/snap-workspace/spectra-trend?ip={IP}&adc=12').status_code == 422


def test_read_budget_preflight_and_cache_invalidation(fixture, monkeypatch):
    client, _, shards, _ = fixture
    save(shards)
    url=f'/api/snap-workspace/spectra-trend?ip={IP}&adc=1'
    assert len(client.get(url).json()['points']) == 1
    save(shards, NOW-10)
    assert len(client.get(url).json()['points']) == 2
    monkeypatch.setattr(module, 'READ_BUDGET', 1)
    assert client.get(url).status_code == 400


def test_bad_manifest_or_lost_shard_is_not_zero_signal(fixture):
    client, store, shards, settings = fixture
    row=save(shards)
    store.execute('UPDATE shards SET shape=? WHERE id=?', (json.dumps([12,3072]),row['id']))
    board=client.get('/api/snap-workspace/spectra').json()['boards'][0]
    assert board['spectra_db'] is None and 'Invalid saved SNAP shard' in board['error']
    store.execute('UPDATE shards SET shape=?,path=? WHERE id=?', (json.dumps([12,4096]),str(settings.store_root/'missing.zarr'),row['id']))
    board=client.get('/api/snap-workspace/spectra').json()['boards'][0]
    assert board['spectra_db'] is None and board['error']


def test_readonly_routes_never_submit_jobs(fixture):
    client, store, shards, _ = fixture
    save(shards)
    for path in ['health','spectra','spectra-catalog',f'spectra-trend?ip={IP}&adc=0']:
        assert client.get('/api/snap-workspace/'+path).status_code == 200
    assert store.query('SELECT COUNT(*) n FROM jobs')[0]['n'] == 0
    assert store.query('SELECT COUNT(*) n FROM watermarks')[0]['n'] == 0


def test_delivery_independent_of_failed_control_and_alignment(fixture, monkeypatch):
    client, _, _, _ = fixture
    monkeypatch.setattr(module, 'latest_reads', lambda _: {IP:dict(ts=NOW-10, programmed=False,
        errors={'period_pps':'unreadable','autocorr':'skipped: programmed=False'})})
    monkeypatch.setattr(module, 'read_latest_vis', lambda _: dict(ts=NOW-300,inputs=[6],vis=np.ones((1,3072))))
    out=client.get('/api/snap-workspace/health').json()
    b=out['boards'][0]
    assert b['control_status']=='unavailable'
    assert b['streaming']['state']=='ok' and out['streaming']['ok']==1
    assert b['pps_status']['period_ok'] is None
    assert b['pps_status']['alignment']=='unknown' and out['pps']['state']=='unknown'
    assert b['production_evidence']['nonzero_inputs']==1


@pytest.mark.parametrize('age,period,expected', [(10,250000000,'unknown'),(10,1,'attention'),(20000,1,'unknown')])
def test_pps_rate_is_not_alignment_and_old_fault_is_stale(fixture,monkeypatch,age,period,expected):
    client, _, _, _ = fixture
    monkeypatch.setattr(module, 'latest_reads', lambda _: {IP:dict(ts=NOW-age,programmed=True,
        pps_raw=dict(period_pps=period,count_pps=100),fs_hz=250000000)})
    out=client.get('/api/snap-workspace/health').json()
    assert out['pps']['state']==expected
    assert out['boards'][0]['pps_status']['alignment']=='unknown'
    assert out['streaming']['state']=='unknown'


@pytest.mark.parametrize('age,inputs,values', [(901,[6],1),(1,[7],1),(1,[6],0),(-100,[6],1)])
def test_streaming_not_green_for_stale_missing_zero_or_future_data(fixture,monkeypatch,age,inputs,values):
    client, _, _, _ = fixture
    monkeypatch.setattr(module, 'read_latest_vis', lambda _: dict(ts=NOW-age,inputs=inputs,vis=np.full((1,3072),values)))
    out=client.get('/api/snap-workspace/health').json()
    assert out['streaming']['state']=='attention'
