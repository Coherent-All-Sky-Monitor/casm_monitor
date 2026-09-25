import base64
import io
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import HTTPException
from PIL import Image

from casm_monitor.web import science_array as array
from casm_monitor.web.science import ScienceRequest, draw_views, validate_request


def test_auto_phase_is_available_and_zero_visibility_remains_undefined():
    req=ScienceRequest(pairs=[(8,8)],t0=0,t1=300,kind='phase_spectrum',reference='raw')
    validate_request(req)
    np.testing.assert_allclose(array.phase(np.array([1+0j,0j])),[0,np.nan],equal_nan=True)


def test_complex_estimators_preserve_signed_values_and_scalar_amplitude():
    z=np.array([1+2j,-1-2j])
    v=array.components(z)
    np.testing.assert_equal(v['real'],[1,-1])
    np.testing.assert_equal(v['imag'],[2,-2])
    mean=array.components(z,axis=0)
    assert mean['real']==mean['imag']==0
    assert np.isnan(mean['phase'])
    assert mean['amp']==pytest.approx(np.sqrt(5))
    p=array.components(np.exp(1j*np.deg2rad([179,-179])),axis=0)['phase']
    assert abs(p)==pytest.approx(np.pi)


def test_preview_keeps_magnitude_when_phase_cancels():
    z=np.array([[[1+0j,-1+0j,2+0j,-2+0j]]])
    out=array.preview_values(z,max_channels=2)
    np.testing.assert_equal(out['amp'],[[[1,2]]])
    np.testing.assert_equal(out['real'],0)
    assert np.isnan(out['phase']).all()


def test_reference_direction_is_consistent():
    inputs=[dict(packet_idx=2),dict(packet_idx=8),dict(packet_idx=18)]
    pairs=[(2,8),(8,8),(8,18)]
    z=np.broadcast_to(np.array([1+2j,9+0j,3+4j])[None,:,None],(2,3,3))
    p=array.make_panels(z,np.array([0,array.DT_S]),np.array([430,420,410]),inputs,pairs,8,'cross')
    np.testing.assert_allclose(p[0]['spectra']['imag']['latest'],[2]*3)
    np.testing.assert_allclose(p[2]['spectra']['imag']['latest'],[-4]*3)
    np.testing.assert_allclose(p[2]['spectra']['phase']['latest'],[-np.arctan2(4,3)]*3)
    assert p[1]['is_auto'] and not p[2]['is_auto']


def test_matrix_is_hermitian_and_uses_magnitude_mean():
    z=np.array([[4,4],[1+2j,-1-2j],[9,9]])
    m=array.matrix_values(z,[(2,2),(2,8),(8,8)],[dict(packet_idx=8),dict(packet_idx=2)])
    np.testing.assert_equal(m['real'],[[9,0],[0,4]])
    assert m['amp'][0][1]==pytest.approx(np.sqrt(5))
    assert m['phase'][0][1] is None
    z[1]=[1+2j,1+2j]
    m=array.matrix_values(z,[(2,2),(2,8),(8,8)],[dict(packet_idx=8),dict(packet_idx=2)])
    assert m['imag'][0][1]==-2 and m['imag'][1][0]==2
    assert m['phase'][0][1]==-m['phase'][1][0]


def test_dynamic_raster_retains_actual_gaps_and_undefined_phase():
    z=np.array([[1+0j,0j],[1j,1j]])
    tile=array.image_tile(array.phase(z),np.array([0,2*array.DT_S]),'phase')
    im=np.asarray(Image.open(io.BytesIO(base64.b64decode(tile['src'].split(',')[1]))))
    assert im.shape==(2,3,3)
    np.testing.assert_equal(im[:,1],[[29,35,43],[29,35,43]])
    np.testing.assert_equal(im[1,0],[29,35,43])


@pytest.mark.parametrize('q',['real','imag','amplitude','phase'])
def test_detail_waterfall_contains_the_requested_values(q):
    import matplotlib.pyplot as plt
    req=ScienceRequest(pairs=[(8,18)],t0=0,t1=300,reference='raw',kind=q+'_waterfall')
    z=np.array([[[1+2j,0j,-3-4j]],[[2+1j,4j,2-3j]]])
    fig=draw_views(req,z,np.array([0,array.DT_S]),np.array([430,420,410]),['baseline'])[0]
    actual=fig.axes[0].collections[0].get_array().reshape(3,2)
    expected=array.components(z)['amp' if q=='amplitude' else q][:,0].T
    np.testing.assert_allclose(actual.filled(np.nan),expected,equal_nan=True)
    plt.close(fig)


@pytest.mark.parametrize('q',['real','imag','amplitude','phase'])
def test_latest_spectrum_does_not_average_old_integrations(q):
    import matplotlib.pyplot as plt
    req=ScienceRequest(pairs=[(8,18)],t0=0,t1=300,reference='raw',kind=q+'_spectrum',spectrum_statistic='latest')
    z=np.array([[[100+200j]*3],[[-1-2j,2-3j,3+4j]]])
    fig=draw_views(req,z,np.array([0,array.DT_S]),np.array([430,420,410]),['baseline'])[0]
    np.testing.assert_allclose(fig.axes[0].lines[0].get_ydata(),array.components(z[-1])['amp' if q=='amplitude' else q][0])
    plt.close(fig)


@pytest.mark.parametrize('change',[dict(hours=25),dict(hours=float('nan')),dict(fmin=500,fmax=400),dict(mode='unknown')])
def test_snapshot_rejects_unbounded_reads(change):
    with pytest.raises(HTTPException):
        array.snapshot(None,None,**change)


def test_snapshot_payload_and_cache(monkeypatch,settings):
    import gzip,json
    from casm_monitor.web import science
    inputs=[dict(packet_idx=8,antenna=9,station='N21E1',position_enu_m=[0,0,0]),
            dict(packet_idx=18,antenna=19,station='N11E2',position_enu_m=[.4,-5,0])]
    times=np.array([1000,1000+array.DT_S]);freq=np.array([430,420,410]);cube=np.ones((2,2,3),complex)
    monkeypatch.setattr(science,'select_layout',lambda *a:dict(id='fixture',path='unused'))
    monkeypatch.setattr(science,'geometry',lambda *a:(inputs,[]))
    monkeypatch.setattr(science,'bounded_rows',lambda *a:[dict(id=1,t1=times[-1])])
    monkeypatch.setattr(science,'load_selection',lambda *a:(cube,times,freq,[],0))
    vs=SimpleNamespace(series=lambda *a,**k:(np.ones((1,3,3),complex),times[-1:],freq,[8,18]))
    monkeypatch.setattr(array,'VisStore',lambda *a:vs)
    reader=SimpleNamespace(query=lambda *a:[dict(t=times[-1])],latest_scalars=lambda:{})
    deployment=dict(inspection_state='complete',product_id='first',positions=[dict(slot=18,antenna=19)])
    monkeypatch.setattr(array,'inspected_deployment',lambda *a:deployment)
    array._CACHE.clear()
    encoded=array.snapshot(settings,reader)
    body=json.loads(gzip.decompress(encoded))
    assert body['default_inputs']==[18] and body['samples']==2
    assert [i['beamforming'] for i in body['inputs']]==[False,True]
    assert body['membership']['product_id']=='first'
    assert body['selection']['hours']==24
    assert len(body['panels'])==2
    monkeypatch.setattr(science,'load_selection',lambda *a:pytest.fail('cached snapshot reread data'))
    assert array.snapshot(settings,reader)==encoded
    # A deployment/membership change must invalidate even unchanged vis shards.
    monkeypatch.setattr(science,'load_selection',lambda *a:(cube,times,freq,[],0))
    deployment.update(product_id='second',positions=[dict(slot=8,antenna=9)])
    changed=json.loads(gzip.decompress(array.snapshot(settings,reader)))
    assert changed['default_inputs']==[8]
    assert changed['membership']['product_id']=='second'
    array._CACHE.clear()


@pytest.mark.parametrize('deployment',[
    dict(inspection_state='pending'),
    dict(inspection_state='complete',positions=[]),
    dict(inspection_state='complete',positions=[dict(slot=8,antenna=18)]),
    dict(inspection_state='complete',positions=[dict(slot=8,antenna=9),dict(slot=99,antenna=19)]),
])
def test_membership_unknown_does_not_fall_back_to_inspection_or_layout(monkeypatch,deployment):
    monkeypatch.setattr(array,'inspected_deployment',lambda *a:deployment)
    inputs=[dict(packet_idx=8,antenna=9,in_bf=True)]
    result=array.beamforming_membership(None,SimpleNamespace(latest_scalars=lambda:{}),inputs)
    assert result['status']=='unknown' and result['inputs']==[]


@pytest.mark.parametrize('change', [dict(pairs=[]), dict(pairs=[(1, 2)]*17),
    dict(pairs=[(1, 2), (2, 1)]), dict(pairs=[(1,)]), dict(t1=86401),
    dict(quantity='coh'), dict(reference='cal'), dict(t0=float('nan')),
    dict(fmin=500, fmax=400)])
def test_pair_batch_rejects_unbounded_or_ambiguous_reads(change):
    args=dict(pairs=[(8, 18)], t0=0, t1=1000)
    args.update(change)
    with pytest.raises(HTTPException) as exc:
        array.pair_snapshot(None, None, **args)
    assert exc.value.status_code == 400


@pytest.mark.parametrize('quantity', ['amp', 'real', 'imag', 'phase'])
def test_pair_batch_is_pinned_bounded_cached_and_conjugated(monkeypatch, settings, quantity):
    import gzip, json
    from casm_monitor.web import science
    inputs=[dict(packet_idx=8, position_enu_m=[0, 0, 0]),
            dict(packet_idx=18, position_enu_m=[1, 1, 0])]
    t=np.array([1000, 1000+array.DT_S, 1000+3*array.DT_S])
    f=np.array([430, 420, 410])
    z=np.array([[[1+2j, -2+3j, 0j]], [[4-1j, 3+2j, 1j]], [[2+3j, 4+2j, 1+0j]]])
    monkeypatch.setattr(science, 'select_layout', lambda *a: dict(id='fixture', path='unused'))
    monkeypatch.setattr(science, 'geometry', lambda *a: (inputs, []))
    def bounds(vstore, stream, t0, t1, pairs):
        assert pairs == [(8, 18)] and (t0, t1) == (t[0], t[-1])
        return [dict(id=1, t1=t[-1])]
    monkeypatch.setattr(science, 'bounded_rows', bounds)
    def load(vstore, req, t0, t1, layout):
        assert req.pairs == [(8, 18)] and req.resolution == 'avg8'
        return z, t, f, [], 0
    monkeypatch.setattr(science, 'load_selection', load)
    monkeypatch.setattr(array, 'VisStore', lambda *a: None)
    array._PAIR_CACHE.clear()
    args=dict(pairs=[(18, 8)], t0=t[0], t1=t[-1], quantity=quantity)
    body=array.pair_snapshot(settings, None, **args)
    result=json.loads(gzip.decompress(body))
    panel=result['panels'][0]
    expected=array.preview_values(z.conj())[quantity][:, 0]
    assert panel['tile'] == array.image_tile(expected, t, quantity)
    assert panel['pair'] == [18, 8] and panel['stored_pair'] == [8, 18]
    assert panel['tile']['width'] == 4 and result['samples'] == 3
    assert (result['t0'], result['t1']) == (t[0], t[-1])
    monkeypatch.setattr(science, 'load_selection', lambda *a: pytest.fail('reread cached batch'))
    assert array.pair_snapshot(settings, None, **args) == body
    with pytest.raises(HTTPException) as exc:
        array.pair_snapshot(settings, None, **{**args, 'pairs': [(8, 99)]})
    assert exc.value.status_code == 400
    array._PAIR_CACHE.clear()
