"""Source history uses bounded native data and the canonical cross-power API."""
from types import SimpleNamespace
import json

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient

from bf_weights_generator import CalibrationWeights
from casm_io.constants import C_LIGHT_M_S
from casm_io.correlator.mapping import AntennaMapping
from casm_monitor.collectors.vis import DT_S, STREAM_FULL
from casm_monitor.observation import file_identity
from casm_monitor.web import science_transit, source_transits as st
from casm_monitor.web.source_history import build_router


def mapping(reverse=False):
    return AntennaMapping(pd.DataFrame(dict(
        antenna_id=[9,19], snap_id=[0,1], adc=[8,6], packet_index=[1,0] if reverse else [0,1],
        x_m=[0.,3.], y_m=[0.,10.], z_m=[0.,1.], functional=[1,1], include_in_beamforming=[1,1])))


@pytest.mark.parametrize('text,expected', [('Sun','sun'),('cyg-a','cyg_a'),('Cyg A','cyg_a'),
    ('CYGNUS_A','cyg_a'),('cas-a','cas_a'),('Cas A','cas_a'),('Cassiopeia A','cas_a'),
    ('tau-a','tau_a'),('Tau A','tau_a'),('Taurus A','tau_a'),('Crab','tau_a'),('B0329',None),('unknown',None)])
def test_aliases(text, expected):
    assert st.source_key(text) == expected


@pytest.mark.parametrize('source,day,code', [('other','2026-09-24',404),('sun','2026-9-24',400),
    ('sun','2026-02-30',400),('sun','2026-09-24T00:00',400)])
def test_invalid_selection(source, day, code):
    with pytest.raises(HTTPException) as error:
        st.transit_time(source, day)
    assert error.value.status_code == code


@pytest.mark.parametrize('reverse', [False,True])
def test_beam_closure_gain_sign_channel_order_missing_and_autos(monkeypatch, reverse):
    import casm_vis_analysis.beam_power as api
    freq = np.array([480.,470.,460.,450.,440.,430.])
    stamps = np.arange(3)*DT_S
    direction = np.array([[.1,.3,.9],[.2,.3,.9],[.3,.3,.9]])
    direction /= np.linalg.norm(direction,axis=1)[:,None]
    monkeypatch.setattr(api,'source_enu',lambda source,t:direction)
    monkeypatch.setattr(api,'source_altaz',lambda source,t:(np.ones(3)*60,np.ones(3)*30))
    # Distinct amplitude and phase per antenna/frequency; ascending calibration
    # rows deliberately reverse the antenna order and native channel ordering.
    weights = np.array([2*np.exp(1j*np.arange(6)*.4), .7*np.exp(-1j*np.arange(6)*.3)])
    cal = CalibrationWeights(weights=weights[::-1,::-1].copy(), flags=np.array([1,1,1,0,1,1],bool)[::-1],
        frequencies_hz=freq[::-1]*1e6, ant_ids=np.array([19,9]), ref_ant_id=9)
    # Calibration with a nonfinite weight also masks that entire channel.
    cal.weights[0,1] = np.nan
    expected = np.array([6.,-4.,10.])[:,None] * np.ones((3,6))
    phase = 2*np.pi*(direction @ np.array([3.,10.,1.]))[:,None]/C_LIGHT_M_S*freq[None,:]*1e6
    baseline = expected/2*np.exp(1j*phase)/(weights[0]*weights[1].conj())
    vis = np.full((3,6,3),1e15+0j)
    vis[:,:,1] = baseline.conj() if reverse else baseline
    vis[2,1,1] = np.nan
    data = dict(vis=vis,freq_mhz=freq,time_unix=stamps)
    power,good = st.beam_spectrum(data,mapping(reverse),cal,'sun')
    np.testing.assert_array_equal(good,[1,1,1,0,0,1])
    expected[:,~good] = np.nan
    expected[2,1] = np.nan
    np.testing.assert_allclose(power,expected,rtol=2e-6,equal_nan=True)
    assert (power[1,good] < 0).all()  # No abs(), auto leakage or double calibration.


@pytest.mark.parametrize('issue', ['shift','all_flagged','shape'])
def test_calibration_mismatch_refused(issue):
    freq = np.array([480.,470.,460.])
    cal = CalibrationWeights(weights=np.ones((2,3),complex),flags=np.ones(3,bool),
        frequencies_hz=freq*1e6,ant_ids=np.array([9,19]),ref_ant_id=9)
    if issue == 'shift':
        cal.frequencies_hz += 1e5
    elif issue == 'all_flagged':
        cal.flags[:] = False
    else:
        cal.weights = cal.weights[:,:2]
    with pytest.raises(HTTPException) as error:
        st.beam_spectrum(dict(freq_mhz=freq),mapping(),cal,'sun')
    assert error.value.status_code == 409


def test_native_keeps_first_file_and_missing_samples(monkeypatch, settings, store):
    stamps = np.arange(4)*DT_S
    freq = 484.375-np.arange(3072)*.030517578125
    rows = [dict(id=i,meta=dict(t=[t],flags=dict(first_of_file=i in (0,2)),
        nchan=3072,freq_top_mhz=freq[0],chan_bw_mhz=.030517578125)) for i,t in enumerate(stamps)]
    z = np.ones((4,3,3072),np.complex64)
    z[1,1,3] = np.nan
    def series(stream,t0,t1,pairs,**kw):
        assert stream == STREAM_FULL and pairs == [(0,0),(0,1),(1,1)]
        assert kw == {'allow_full_fallback':False}
        return z,stamps,freq,[0,1]
    monkeypatch.setattr(science_transit,'VisStore',lambda *a:SimpleNamespace(
        shards=SimpleNamespace(list=lambda *a,**k:rows),series=series))
    data,_,_,_ = science_transit.native_inputs(store,mapping(),[9,19],0,stamps[-1],settings,preserve_missing=True)
    np.testing.assert_array_equal(data['time_unix'],stamps)
    assert np.isnan(data['vis'][1,3,1])
    ordinary,_,_,_ = science_transit.native_inputs(store,mapping(),[9,19],0,stamps[-1],settings)
    np.testing.assert_array_equal(ordinary['time_unix'],stamps[[0,2,3]])


@pytest.mark.parametrize('issue',['averaged','too_many','mixed_axis','read_budget'])
def test_native_limits_before_read(monkeypatch,settings,store,issue):
    samples = 56 if issue=='too_many' else 53 if issue=='mixed_axis' else 55
    nchan = 384 if issue=='averaged' else 3072
    rows = [dict(id=1,meta=dict(t=(np.arange(samples)*DT_S).tolist(),nchan=nchan,
        freq_top_mhz=484.375,chan_bw_mhz=.030517578125))]
    if issue=='mixed_axis':
        rows.append(dict(id=2,meta=dict(t=[9000],nchan=3072,freq_top_mhz=484.,chan_bw_mhz=.030517578125)))
    def no_read(*args,**kwargs):
        pytest.fail('Oversized/non-native request reached the array reader')
    monkeypatch.setattr(science_transit,'VisStore',lambda *a:SimpleNamespace(
        shards=SimpleNamespace(list=lambda *a,**k:rows),series=no_read))
    ants=list(range(32 if issue=='read_budget' else 2))
    with pytest.raises(HTTPException) as error:
        science_transit.native_inputs(store,SimpleNamespace(packet_index=lambda a:a),ants,0,9000,settings,preserve_missing=True)
    assert error.value.status_code==400


def test_catalog_native_dates_and_no_future_transit(monkeypatch, settings):
    from datetime import datetime, timezone
    last = datetime(2026,9,24,23,tzinfo=timezone.utc).timestamp()
    reader = SimpleNamespace(query=lambda sql,args:[dict(t0=last-2*86400,t1=last)])
    def peak(source,date):
        return datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp()+22*3600
    monkeypatch.setattr(st,'transit_time',peak)
    monkeypatch.setattr(st,'calibration_catalog',lambda s:[dict(id='a'*32,path='/cal/current.h5')])
    def shards(stream,t0,t1):
        assert stream == STREAM_FULL
        mid=(t0+t1)/2
        times=[t for t in [mid-DT_S,mid,mid+DT_S] if last-2*86400 <= t <= last]
        return [dict(meta=dict(t=times))] if times else []
    monkeypatch.setattr(st,'VisStore',lambda *a:SimpleNamespace(shards=SimpleNamespace(list=shards)))
    result = st.catalog(settings,reader,'sun')
    assert [r['date'] for r in result['transits']] == ['2026-09-24','2026-09-23']
    assert all(r['partial'] for r in result['transits'])
    assert result['calibration']['name'] == 'current.h5'
    assert result['coverage']['stream'] == STREAM_FULL
    monkeypatch.setattr(st,'transit_time',lambda source,date:peak(source,date)+7200)
    assert st.catalog(settings,reader,'sun')['transits'][0]['date'] == '2026-09-23'


@pytest.fixture
def snapshot_fixture(monkeypatch,tmp_path):
    import bf_weights_generator
    st._CACHE.clear()
    calpath,layoutpath = tmp_path/'cal.h5',tmp_path/'layout.csv'
    calpath.write_text('cal fixture')
    layoutpath.write_text('layout fixture')
    item = dict(id='a'*32,**file_identity(calpath))
    monkeypatch.setattr(st,'calibration_catalog',lambda s:[item])
    monkeypatch.setattr(st,'transit_time',lambda *a:5000)
    monkeypatch.setattr(st,'select_layout',lambda *a:dict(path=str(layoutpath),id='layout'))
    monkeypatch.setattr(AntennaMapping,'load',lambda *a:mapping())
    cal = SimpleNamespace(ant_ids=np.array([9,19]))
    monkeypatch.setattr(bf_weights_generator,'load_calibration_weights',lambda *a:cal)
    rows = [dict(id=1,t1=5300,meta=dict(t=(5000+np.arange(3)*DT_S).tolist()))]
    monkeypatch.setattr(st,'VisStore',lambda *a:SimpleNamespace(shards=SimpleNamespace(list=lambda *a,**k:rows)))
    calls = []
    def native(*args,**kwargs):
        calls.append(kwargs)
        return dict(time_unix=5000+np.arange(3)*DT_S,freq_mhz=np.array([480.,470.,460.])),mapping(),rows,1234
    monkeypatch.setattr(st,'native_inputs',native)
    monkeypatch.setattr(st,'beam_spectrum',lambda *a,**k:(np.array([[1,2,3],[-1,-2,-3],[4,5,6]]),np.ones(3,bool)))
    yield item,cal,calls,rows,calpath
    st._CACHE.clear()


def test_snapshot_current_cal_cache_and_signed_preview(snapshot_fixture, settings, store):
    item,cal,calls,rows,_ = snapshot_fixture
    result = st.snapshot(settings,store,'sun','2026-09-24',item['id'])
    json.dumps(jsonable_encoder(result),allow_nan=False)
    assert result['preview']['cross_power'][1] == [-1.,-2.,-3.]
    assert result['light_curve']['cross_power'] == [2.,-2.,5.]
    assert result['light_curve']['time_unix'] == result['preview']['time_unix']
    assert result['light_curve']['channels'] == 3
    assert result['light_curve']['freq_range_mhz'] == [460.,480.]
    assert result['tile']['min'] < 0 < result['tile']['max']
    assert result['antenna_ids'] == [9,19]
    assert result['details']['stream'] == STREAM_FULL
    assert result['details']['background'] == 'No static or off-source subtraction'
    assert calls == [dict(preserve_missing=True)]
    assert st.snapshot(settings,store,'sun','2026-09-24',item['id']) is result
    assert len(calls) == 1
    rows[0]['t1'] += DT_S
    st.snapshot(settings,store,'sun','2026-09-24',item['id'])
    assert len(calls) == 2
    with pytest.raises(HTTPException) as error:
        st.snapshot(settings,store,'sun','2026-09-24','b'*32)
    assert error.value.status_code == 409 and len(calls) == 2


@pytest.mark.parametrize('source', list(st.SOURCES))
def test_native_band_mean_flags_missing_and_source_identity(snapshot_fixture, monkeypatch, settings, store, source):
    item,_,_,_,_ = snapshot_fixture
    power = np.array([[2.,900.,6.],[-2.,800.,-6.],[4.,700.,np.nan]])
    monkeypatch.setattr(st,'beam_spectrum',lambda *a,**k:(power,np.array([True,False,True])))
    result = st.snapshot(settings,store,source,'2026-09-24',item['id'])
    assert result['source'] == source and result['name'] == st.SOURCES[source]
    assert result['light_curve']['cross_power'] == [4.,-4.,None]
    assert result['light_curve']['channels'] == 2
    json.dumps(jsonable_encoder(result),allow_nan=False)


def test_no_silent_antenna_subset(snapshot_fixture, settings, store):
    item,cal,calls,_,_ = snapshot_fixture
    cal.ant_ids = np.array([9,19,42])
    with pytest.raises(HTTPException) as error:
        st.snapshot(settings,store,'sun','2026-09-24',item['id'])
    assert error.value.status_code == 409 and calls == []


def test_changed_cal_during_read_refused(snapshot_fixture, monkeypatch, settings, store):
    item,_,_,_,calpath = snapshot_fixture
    def beam(*a,**k):
        calpath.write_text('changed calibration fixture')
        return np.ones((3,3)),np.ones(3,bool)
    monkeypatch.setattr(st,'beam_spectrum',beam)
    with pytest.raises(HTTPException) as error:
        st.snapshot(settings,store,'sun','2026-09-24',item['id'])
    assert error.value.status_code == 409 and not st._CACHE


def test_stationary_phasing_is_fixed_not_tracking(monkeypatch):
    import casm_vis_analysis.beam_power as api
    from casm_vis_analysis.sources import source_enu
    freq=np.array([470.,450.,430.])
    times=1_790_000_000+np.arange(3)*1200
    # Inject a moving point source; phase only to its middle-time direction.
    direction=source_enu('cyg_a',times)
    baseline=np.array([3.,10.,1.])
    phase=2*np.pi*(direction@baseline)[:,None]/C_LIGHT_M_S*freq[None,:]*1e6
    vis=np.zeros((3,3,3),complex);vis[:,:,1]=np.exp(1j*phase)
    cal=CalibrationWeights(weights=np.ones((2,3),complex),flags=np.ones(3,bool),
        frequencies_hz=freq*1e6,ant_ids=np.array([9,19]),ref_ant_id=9)
    from casm_vis_analysis.sources import source_altaz
    alt,az=source_altaz('cyg_a',times[1:2])
    fixed,good=st.beam_spectrum(dict(vis=vis,time_unix=times,freq_mhz=freq),mapping(),cal,'cyg_a',pointing=(alt[0],az[0]))
    expected=2*np.cos(phase-phase[1])
    np.testing.assert_allclose(fixed,expected,atol=1e-6)
    assert not np.allclose(fixed[0],fixed[1])
    np.testing.assert_allclose(fixed[1],2,atol=1e-6)


def test_four_hour_window_streams_small_chunks(monkeypatch,settings,store):
    stamps=5000+np.arange(105)*DT_S
    rows=[dict(id=1,meta=dict(t=stamps.tolist()))]
    bounds=[]
    def native(reader,m,ants,t0,t1,*args,**kwargs):
        times=stamps[(stamps>=t0)&(stamps<=t1)]
        assert 3<=len(times)<=26
        bounds.append((t0,t1))
        return dict(time_unix=times,freq_mhz=np.array([480.,470.,460.])),mapping(),rows,100
    monkeypatch.setattr(st,'native_inputs',native)
    seen=[]
    def beam(data,m,cal,source,*,pointing):
        seen.append(pointing)
        return np.ones((len(data['time_unix']),3)),np.ones(3,bool)
    monkeypatch.setattr(st,'beam_spectrum',beam)
    power,good,times,freq,used,size=st.window_spectrum(settings,store,mapping(),[9,19],None,'cas_a',(60.,0.),rows,stamps[0],stamps[-1])
    np.testing.assert_array_equal(times,stamps)
    assert power.shape==(105,3) and len(bounds)==5 and size==500
    assert seen==[(60.,0.)]*5 and len(used)==1


def test_router_separates_beams_pdmp_and_validates_identity(monkeypatch,settings,store,tmp_path):
    app = FastAPI()
    app.state.settings,app.state.reader = settings,store
    app.include_router(build_router(tmp_path))
    monkeypatch.setattr(st,'catalog',lambda s,r,name:dict(kind='visibility_beam',source=name))
    monkeypatch.setattr(st,'snapshot',lambda s,r,source,day,cid:dict(source=source,date=day,calibration_id=cid))
    with TestClient(app) as client:
        assert client.get('/api/sources?q=cyg-a').json()['source'] == 'cyg_a'
        assert client.get('/api/sources?q=sun').json()['kind'] == 'visibility_beam'
        assert client.get('/api/sources?q=cas-a').json()['source'] == 'cas_a'
        assert client.get('/api/sources?q=tau-a').json()['source'] == 'tau_a'
        assert 'kind' not in client.get('/api/sources?q=B0329').json()
        url = '/api/sources/transits/sun/2026-09-24'
        assert client.get(url,params=dict(calibration_id='../../cal.h5')).status_code == 422
        assert client.get(url,params=dict(calibration_id='a'*32)).json()['calibration_id'] == 'a'*32
        assert client.post(url).status_code == 405
