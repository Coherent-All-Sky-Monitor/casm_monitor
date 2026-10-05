import json
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from casm_monitor import vis_ops
from casm_monitor.collectors.vis import DT_S, STREAM_AVG8
from casm_monitor.web import visibility_playback as movie, science
from casm_monitor.web.science_array import components, preview_values


@pytest.fixture
def saved(monkeypatch, settings, store, shard_writer):
    inputs = [dict(packet_idx=i, antenna=i+1, station=s, position_enu_m=p, snap=k, slot='A', adc=0)
              for k,(i,s,p) in enumerate([(2,'N21E1',[0,0,0]),(8,'N11E1',[0,-5,0]),(18,'N01E1',[0,-10,0])])]
    monkeypatch.setattr(science, 'select_layout', lambda *a: dict(id='fixture',path='unused'))
    monkeypatch.setattr(science, 'geometry', lambda *a: (inputs,[]))
    monkeypatch.setattr(movie, 'beamforming_membership', lambda *a: dict(status='complete',inputs=[8,18]))
    monkeypatch.setattr(movie, 'sidereal_times', lambda t: ['01:02:03']*len(t))
    times=1790000000.+np.arange(72)*DT_S;times[40:]+=DT_S  # actual missing integration
    rng=np.random.default_rng(13)
    z=(rng.normal(size=(72,6,384))+1j*rng.normal(size=(72,6,384))).astype('complex64')
    z[0,1,0]=0;z[1,1,1]=complex(np.nan,np.nan)
    for k in range(0,72,8):
        shard_writer.write(STREAM_AVG8,z[k:k+8],t0=times[k],t1=times[k+7],
                           meta=dict(t=times[k:k+8].tolist(),inputs=[2,8,18],nchan=384,
                                     freq_top_mhz=484.25,chan_bw_mhz=.244140625))
    service=movie.PlaybackStore(settings,store)
    return service,z,times,inputs


def decode(data):
    n=int.from_bytes(data[:4],'little');meta=json.loads(data[4:4+n])
    return meta,np.frombuffer(data[4+n:],dtype='<f4').reshape(meta['shape'])


def test_manifest_pins_times_keeps_gaps_and_current_membership(saved):
    service,z,times,inputs=saved
    m=service.manifest(inputs=[2,8,18],mode='cross',reference_input=8)
    assert m['pairs']==[(2,8),(8,8),(18,8)]
    np.testing.assert_equal(m['times'],times)
    assert m['times'][40]-m['times'][39]==pytest.approx(2*DT_S)
    assert len(m['freq_mhz'])==384 and len(m['preview_freq_mhz'])==128
    assert [i['beamforming'] for i in m['inputs']]==[False,True,True]
    assert m['lst']==['01:02:03']*72
    assert service.manifest(inputs=[2,8,18],mode='cross',reference_input=8) is m
    assert len(service.manifests)==1 and service.cache_bytes<=movie.CACHE_BYTES


@pytest.mark.parametrize('ref',[2,8,18])
@pytest.mark.parametrize('encoding',['complex','preview'])
def test_chunks_match_static_quantities_and_reference_conjugation(saved,ref,encoding):
    service,z,times,_=saved
    m=service.manifest(inputs=[2,8,18],mode='cross',reference_input=ref)
    ranks={(2,2):0,(2,8):1,(2,18):2,(8,8):3,(8,18):4,(18,18):5}
    expected=np.stack([z[:,ranks[tuple(sorted(p))]].conj() if p[0]>p[1] else z[:,ranks[tuple(p)]] for p in m['pairs']],axis=1)
    for base in (0,32,64):
        meta,actual=decode(service.batch(m['id'],base,encoding))
        wanted=expected[base:base+32]
        assert meta['times']==times[base:base+32].tolist()
        if encoding=='complex':
            np.testing.assert_allclose(actual[...,0]+1j*actual[...,1],wanted,equal_nan=True)
        else:
            v=preview_values(wanted)
            for k,q in enumerate(('real','imag','amp')):
                np.testing.assert_allclose(actual[...,k],v[q],rtol=1e-6,equal_nan=True)


def test_sun_uses_existing_transform_before_conjugation(saved):
    service,z,times,inputs=saved
    m=service.manifest(inputs=[18],mode='cross',reference_input=2,reference='sun')
    _,v=decode(service.batch(m['id'],0,'complex'))
    expected=vis_ops.fringe_stop_sun(z[:32,2:3],np.array(m['freq_mhz']),np.array([inputs[0]['position_enu_m'],inputs[2]['position_enu_m']]),[(0,1)],times[:32]).conj()
    np.testing.assert_allclose(v[...,0]+1j*v[...,1],expected,rtol=1e-6,atol=1e-7)


def test_matrix_is_upper_triangle_with_full_data_spectral_limits(saved):
    service,z,_,_=saved
    m=service.manifest(inputs=[2,8,18],mode='matrix')
    assert m['pairs']==[(2,2),(2,8),(2,18),(8,8),(8,18),(18,18)]
    for k,limits in enumerate(m['limits']):
        for q,values in components(z[:,k]).items():
            finite=values[np.isfinite(values)];lo,hi=limits['spectrum'][q]
            assert np.min(finite)>=lo and np.max(finite)<=hi
        assert limits['dynamic']['phase']==[-np.pi,np.pi]


def test_power_limits_use_real_auto_not_magnitude_and_db_after_averaging(saved):
    service,z,_,_=saved
    m=service.manifest(inputs=[2,8,18],mode='auto')
    for k,source in enumerate([0,3,5]):
        values=z[:,source].real
        valid=values[np.isfinite(values)]
        spectrum=m['limits'][k]['spectrum']
        assert spectrum['power'][0]<=valid.min()<0<valid.max()<=spectrum['power'][1]
        positive=valid[valid>0]
        assert spectrum['power_log'][0]<positive.min()<=positive.max()<spectrum['power_log'][1]
        preview=preview_values(z[:,source:source+1])['real']
        expected=10*np.log10(preview[preview>0])
        np.testing.assert_allclose(m['limits'][k]['dynamic']['power_db'],np.percentile(expected,[2,98]),rtol=1e-6)


def test_binary_budgets_eviction_and_unknown_session(saved,monkeypatch):
    service,_,_,_=saved
    monkeypatch.setattr(movie,'CACHE_BYTES',800000)
    m=service.manifest(inputs=[2,8,18],mode='matrix')
    assert service.cache_bytes<=800000
    for base in (0,32,64,0):
        service.batch(m['id'],base,'complex');assert service.cache_bytes<=800000
    for base,encoding in [(-1,'complex'),(1,'complex'),(96,'complex'),(0,'png')]:
        with pytest.raises(HTTPException):service.batch(m['id'],base,encoding)
    with pytest.raises(HTTPException) as exc:service.batch('unknown',0,'complex')
    assert exc.value.status_code==410


@pytest.mark.parametrize('change',[dict(inputs=[]),dict(inputs=[2,2]),dict(inputs=[99]),dict(span_days=30),
    dict(window_hours=25),dict(fmin=500),dict(end=float('nan')),dict(mode='bad'),dict(reference='cal')])
def test_invalid_selection_refused_before_array_reads(saved,change,monkeypatch):
    service,_,_,_=saved
    monkeypatch.setattr(service,'cube',lambda *a:pytest.fail('Read arrays for invalid request'))
    with pytest.raises(HTTPException):service.manifest(**(dict(inputs=[2])|change))


def test_pinned_rows_do_not_pick_up_new_shards(saved,shard_writer):
    service,z,times,_=saved
    m=service.manifest(inputs=[2]);service.chunks.clear();service.cache_bytes=0
    later=times[-1]+DT_S
    shard_writer.write(STREAM_AVG8,z[:1],t0=later,meta=dict(t=[later],inputs=[2,8,18],nchan=384,freq_top_mhz=484.25,chan_bw_mhz=.244140625))
    meta,v=decode(service.batch(m['id'],64,'complex'))
    assert len(v)==8 and meta['times'][-1]==times[-1]
    assert service.manifest(inputs=[2])['id']!=m['id']


def test_metadata_axis_mismatch_rejected_not_filled(saved,store):
    service,_,_,_=saved
    row=store.query('SELECT id,meta FROM shards LIMIT 1')[0]
    meta=json.loads(row['meta']);meta['t']=meta['t'][:-1]
    store.execute('UPDATE shards SET meta=? WHERE id=?',(json.dumps(meta),row['id']))
    with pytest.raises(HTTPException) as exc:service.manifest(inputs=[2])
    assert exc.value.status_code==409


def test_read_only_api_routes(saved,settings,store):
    service,_,_,_=saved
    app=FastAPI();router=APIRouter(prefix='/api/science');movie.register_routes(router,settings,store);app.include_router(router)
    client=TestClient(app)
    before=store.query('SELECT COUNT(*) AS n FROM shards')[0]['n']
    assert client.get('/api/science/array/history').status_code==200
    r=client.get('/api/science/array/playback',params=dict(inputs='2,8',mode='cross',reference_input=18))
    assert r.status_code==200,r.text
    m=r.json();r=client.get('/api/science/array/playback-chunk',params=dict(id=m['id'],base=0,encoding='preview'))
    assert r.status_code==200 and r.headers['content-type']=='application/octet-stream'
    assert client.post('/api/science/array/playback',json={}).status_code==405
    assert store.query('SELECT COUNT(*) AS n FROM shards')[0]['n']==before
