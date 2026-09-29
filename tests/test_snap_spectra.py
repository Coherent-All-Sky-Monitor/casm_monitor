"""Stored 4096-channel views: no hardware, Kafka, writes or gap fabrication."""
import json
import struct

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


def test_history_epoch_hides_but_never_deletes_older_reads(fixture):
    client, store, shards, settings = fixture
    save(shards,NOW-20000)
    save(shards,NOW-100)
    settings.observation_cache_root.mkdir(parents=True,exist_ok=True)
    (settings.observation_cache_root/'snap_history_epoch.json').write_text(json.dumps({'version':1,'start':NOW-1000}))
    current = client.get('/api/snap-workspace/spectra-catalog').json()
    assert current['history_start'] == NOW-1000 and current['reads'] == 1
    assert client.get('/api/snap-workspace/spectra-catalog?archive=true').json()['reads'] == 2
    assert client.get(f'/api/snap-workspace/spectra?at={NOW-19900}').json()['boards'][0]['ts'] is None
    assert client.get(f'/api/snap-workspace/spectra?at={NOW-19900}&archive=true').json()['boards'][0]['ts'] == NOW-20000
    url = f'/api/snap-workspace/spectra-trend?ip={IP}&adc=0'
    assert len(client.get(url).json()['points']) == 1
    assert len(client.get(url+'&archive=true').json()['points']) == 2
    assert store.query("SELECT COUNT(*) AS n FROM shards WHERE stream='snap_read'")[0]['n'] == 2


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


def test_exact_ids_pin_reads_without_age_limit_or_borrowing(fixture):
    client, store, shards, _ = fixture
    old=save(shards,NOW-400*86400, np.ones((12,4096),dtype='float32'))
    save(shards,NOW-100,np.full((12,4096),100,dtype='float32'))
    result=client.get(f'/api/snap-workspace/spectra?ids={old["id"]}').json()
    board=result['boards'][0]
    assert board['spectra_db'][0][0]==0 and board['historical'] and not board['stale']
    assert board['shard_id']==old['id']
    assert result['selected_ids']==str(old['id'])
    for ids in ('x','0','1,2,3,4,5',f'{old["id"]},{old["id"]}'):
        assert client.get('/api/snap-workspace/spectra',params={'ids':ids}).status_code==400
    other=save(shards,NOW-50)
    assert client.get(f'/api/snap-workspace/spectra?ids={old["id"]},{other["id"]}').status_code==400
    assert client.get('/api/snap-workspace/spectra?ids=999999').status_code==404
    foreign=shards.write('other',np.ones((12,4096)),t0=NOW)
    assert client.get(f'/api/snap-workspace/spectra?ids={foreign["id"]}').status_code==404
    assert store.query('SELECT count(*) n FROM jobs')[0]['n']==0


def test_dated_catalog_all_archive_and_trend_anchoring(fixture):
    from datetime import datetime, timezone
    client, _, shards, _ = fixture
    past=NOW-400*86400
    row=save(shards,past)
    save(shards,NOW-100)
    day=datetime.fromtimestamp(past,timezone.utc).strftime('%Y-%m-%d')
    dates=client.get('/api/snap-workspace/spectra-days').json()['days']
    assert len(dates)==2 and dates[-1]['day']==day
    out=client.get('/api/snap-workspace/spectra-catalog',params={'day':day}).json()
    assert out['archive'] and out['day']==day
    assert out['snapshots'][0]['ids']==[row['id']]
    assert out['reads']==1
    assert client.get('/api/snap-workspace/spectra-catalog?day=2026-02-30').status_code==400
    out=client.get('/api/snap-workspace/spectra-trend',params={'ip':IP,'adc':0,'archive':True,'at':past+100}).json()
    assert [p['shard_id'] for p in out['points']]==[row['id']]


def test_changes_current_saved_evidence_only_no_writes(fixture, monkeypatch):
    client,store,shards,_=fixture
    for k in range(12):
        a=np.ones((12,4096),dtype='float32')
        if k>=10: a[6]*=10
        save(shards,NOW-100-(11-k)*3600,a)
    out=client.get('/api/snap-workspace/changes').json()
    assert out['inputs'][6]['state']=='attention'
    assert out['inputs'][0]['state']=='stable'
    assert store.query('SELECT count(*) n FROM jobs')[0]['n']==0
    assert store.query('SELECT count(*) n FROM watermarks')[0]['n']==0
    monkeypatch.setattr(module,'latest_reads',lambda _: {IP:dict(ts=NOW-1,errors={'autocorr':'failed'})})
    out=client.get('/api/snap-workspace/changes').json()
    assert out['inputs'][6]['state']=='unknown' and out['inputs'][6]['label']=='Latest read failed'


def test_selected_missing_board_is_blank(fixture):
    client,_,shards,settings=fixture
    second='192.168.120.51'
    with settings.snap_map_csv.open('a') as f: f.write(f'1,I,1,{second}\n')
    old=save(shards,NOW-1000,ip=second)
    current=save(shards,NOW-100)
    out=client.get(f'/api/snap-workspace/spectra?ids={current["id"]}').json()
    b=next(b for b in out['boards'] if b['ip']==second)
    assert b['spectra_db'] is None and b['ts'] is None and b['shard_id'] is None


def test_change_detail_uses_pinned_exact_median_and_is_read_only(fixture):
    client,store,shards,_=fixture
    ids=[]
    for k in range(12):
        a=np.ones((12,4096),dtype='float32')
        if k in (2,10,11): a[6,123]=100
        ids.append(save(shards,NOW-100-(11-k)*3600,a)['id'])
    original=client.get('/api/snap-workspace/changes').json()['inputs'][6]
    assert original['state']=='info'
    path='/api/snap-workspace/change-detail'
    params={'ids':','.join(map(str,original['sample_ids'])),'adc':6}
    out=client.get(path,params=params).json()
    assert out['baseline_ids']==ids[:-2] and out['current_id']==ids[-1]
    assert out['reference_spectra_db'][123]==0
    assert out['current_spectra_db'][123]==20 and out['difference_db'][123]==20
    assert out['peaks'][0]['prior_reads']==1
    # New data cannot move a comparison that is already open.
    save(shards,NOW,np.full((12,4096),10,dtype='float32'))
    assert client.get(path,params=params).json()==out
    for invalid in ('','x','0',f'{ids[0]},{ids[0]}',','.join(map(str,range(1,130)))):
        assert client.get(path,params={'ids':invalid,'adc':6}).status_code==400
    assert client.get(path,params={'ids':'999999','adc':6}).status_code==404
    assert client.get(path,params={'ids':str(ids[0]),'adc':12}).status_code==422
    assert store.query('SELECT count(*) n FROM jobs')[0]['n']==0
    assert store.query('SELECT count(*) n FROM watermarks')[0]['n']==0


def test_playback_day_exact_extrema_ids_and_readonly(fixture):
    from datetime import datetime, timezone
    client,store,shards,_=fixture
    day=datetime.fromtimestamp(NOW,timezone.utc).date().isoformat()
    start=NOW//86400*86400
    values=np.full((12,4096),1e-8,dtype='float64')
    values[6,0]=1e-3  # Include a single edge-channel peak, no percentile limits.
    values[6,4095]=1e-20
    values[5,0]=0;values[5,1]=np.nan
    first=save(shards,start+100,values)
    second=save(shards,start+7300)
    foreign=save(shards,start+7400,ip='not-configured')
    path=f'/api/snap-workspace/spectra-playback?day={day}'
    out=client.get(path).json()
    assert out['span_days']==1 and out['start']==start
    assert [s['ids'] for s in out['snapshots']]==[[first['id']],[second['id']]]
    assert out['limits_db'][f'{IP}/6']==[-200.,-30.]
    assert out['limits_db'][f'{IP}/5'][0]==pytest.approx(-80.)
    assert not out['unavailable_ids']
    newer=save(shards,start+10000,np.full((12,4096),100,dtype='float32'))
    updated=client.get(path).json()
    assert updated['snapshots'][-1]['ids']==[newer['id']]
    assert updated['limits_db'][f'{IP}/6']==[-200.,20.]
    # A pinned frame's arrays do not change when later acquisition arrives.
    pinned=client.get(f'/api/snap-workspace/spectra?ids={first["id"]}').json()
    assert pinned['boards'][0]['spectra_db'][6][0]==-30.
    assert store.query('SELECT count(*) n FROM jobs')[0]['n']==0
    assert store.query('SELECT count(*) n FROM watermarks')[0]['n']==0


def test_playback_window_bounds_archive_gaps_empty_and_errors(fixture,monkeypatch):
    from datetime import datetime, timezone
    client,store,shards,settings=fixture
    day=datetime.fromtimestamp(NOW,timezone.utc).date().isoformat()
    older=save(shards,NOW-6*86400)
    recent=save(shards,NOW-47*3600)
    latest=save(shards,NOW-100)
    path='/api/snap-workspace/spectra-playback'
    for span,expected in [(2,[recent['id'],latest['id']]),(7,[older['id'],recent['id'],latest['id']])]:
        out=client.get(path,params={'day':day,'span_days':span}).json()
        assert out['end']==NOW and out['start']==NOW-span*86400
        assert [s['ids'][0] for s in out['snapshots']]==expected
    assert client.get(path,params={'day':'2000-01-01'}).json()['snapshots']==[]
    for invalid in ['nonsense','2026-02-30']:
        assert client.get(path,params={'day':invalid}).status_code==400
    assert client.get(path,params={'day':day,'span_days':3}).status_code==400
    assert client.get(path,params={'day':day,'span_days':8}).status_code==422
    monkeypatch.setattr(module,'READ_BUDGET',1)
    assert client.get(path,params={'day':day}).status_code==400


def test_playback_missing_shard_and_midnight_do_not_borrow_data(fixture):
    from datetime import datetime,timezone
    client,store,shards,settings=fixture
    second='192.168.120.51'
    with settings.snap_map_csv.open('a') as f:f.write(f'1,I,1,{second}\n')
    midnight=NOW//86400*86400
    before=save(shards,midnight-3)
    after=save(shards,midnight+3,ip=second)
    day=datetime.fromtimestamp(NOW,timezone.utc).date().isoformat()
    store.execute("UPDATE shards SET path=? WHERE id=?",(str(settings.store_root/'gone.zarr'),after['id']))
    out=client.get('/api/snap-workspace/spectra-playback',params={'day':day,'span_days':2}).json()
    assert len(out['snapshots'])==2
    assert out['snapshots'][0]['ids']==[before['id']]
    assert out['snapshots'][1]['ids']==[after['id']]
    assert out['unavailable_ids']==[after['id']]
    pinned=client.get(f'/api/snap-workspace/spectra?ids={after["id"]}').json()
    assert all(b['spectra_db'] is None for b in pinned['boards'])


def unpack_playback(response):
    assert response.status_code==200,response.text[:200]
    assert response.headers['content-type']=='application/octet-stream'
    size=struct.unpack('<I',response.content[:4])[0]
    header=json.loads(response.content[4:4+size])
    data=np.frombuffer(response.content,dtype='<f4',offset=4+size).reshape(header['shape'])
    return header,data


def test_packed_playback_selected_channels_exact_ids_and_readonly(fixture,monkeypatch):
    client,store,shards,_=fixture
    values=np.arange(1,12*4096+1,dtype='float32').reshape(12,4096)*1e-8
    values[6,:4]=[0,np.nan,-1,np.inf]
    first=save(shards,NOW-1000,values)
    second=save(shards,NOW-100,values*10,epoch='e2')
    def forbidden(*_,**__):raise AssertionError('Playback must not load current health or contact hardware')
    monkeypatch.setattr(module,'latest_reads',forbidden)
    monkeypatch.setattr(module,'read_latest_vis',forbidden)
    response=client.get('/api/snap-workspace/spectra-playback-frames',params={
        'frames':f'{first["id"]};{second["id"]}','inputs':f'{IP}/6,{IP}/2'})
    header,data=unpack_playback(response)
    assert header['inputs']==[f'{IP}/6',f'{IP}/2']
    assert header['shape']==[2,2,4096]
    assert [f['ids'] for f in header['frames']]==[[first['id']],[second['id']]]
    assert header['frames'][1]['boards'][0]['eq_epoch']=='e2'
    assert header['frames'][0]['boards'][0]['feng_id']==0
    assert header['frames'][0]['boards'][0]['zero_channels']['6']==1
    assert header['frames'][0]['boards'][0]['invalid_channels']['6']==3
    assert header['freq_mhz'][0]==500 and header['freq_mhz'][-1]==pytest.approx(375.030517578125)
    np.testing.assert_allclose(data[0,0],module.power_db(values[6]),atol=1e-5)
    np.testing.assert_allclose(data[1,1],module.power_db(values[2]*10),atol=1e-5)
    assert store.query('SELECT count(*) n FROM jobs')[0]['n']==0
    assert store.query('SELECT count(*) n FROM watermarks')[0]['n']==0


def test_packed_playback_missing_board_and_broken_shard_are_not_borrowed(fixture):
    client,store,shards,settings=fixture
    second='192.168.120.51'
    with settings.snap_map_csv.open('a') as f:f.write(f'1,I,1,{second}\n')
    save(shards,NOW-200,ip=second)
    good=save(shards,NOW-100)
    bad=save(shards,NOW-50)
    store.execute('UPDATE shards SET path=? WHERE id=?',(str(settings.store_root/'gone.zarr'),bad['id']))
    header,data=unpack_playback(client.get('/api/snap-workspace/spectra-playback-frames',params={
        'frames':f'{good["id"]};{bad["id"]}','inputs':f'{IP}/0,{second}/0'}))
    assert np.isfinite(data[0,0]).all() and np.isnan(data[0,1]).all() and np.isnan(data[1]).all()
    assert header['frames'][0]['boards'][1]['ts'] is None
    assert header['frames'][1]['boards'][0]['error']=='Saved spectrum unavailable'


def test_packed_playback_rejects_invalid_or_oversized_selections(fixture):
    client,store,shards,_=fixture
    a=save(shards,NOW-100)['id'];b=save(shards,NOW-50)['id']
    path='/api/snap-workspace/spectra-playback-frames'
    for frames in ['', 'x', '0', f'{a},{a}', f'{a},{b}', ';'.join([str(a)]*13)]:
        assert client.get(path,params={'frames':frames,'inputs':f'{IP}/0'}).status_code==400
    for inputs in ['', f'{IP}/12', f'{IP}/-1', 'unknown/0', f'{IP}/0,{IP}/0']:
        assert client.get(path,params={'frames':str(a),'inputs':inputs}).status_code==400
    assert client.get(path,params={'frames':'999999','inputs':f'{IP}/0'}).status_code==404
    store.execute('UPDATE shards SET shape=? WHERE id=?',(json.dumps([12,4096,10000]),a))
    assert client.get(path,params={'frames':str(a),'inputs':f'{IP}/0'}).status_code==409
