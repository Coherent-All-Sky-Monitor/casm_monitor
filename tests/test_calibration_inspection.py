import json
from pathlib import Path
import numpy as np
import pytest

from casm_monitor.web.calibration_inspection import snapshot, latest_inspection
from casm_monitor.web.calibration_workspace import action_params


@pytest.fixture
def saved(tmp_path):
    dest=tmp_path/'actions'/'test'/'inspection'
    dest.mkdir(parents=True)
    meta=dict(antennas=[9,12],ref_ant=9,stages=['raw','geometric','svd'],schemes={'uniform':True},
              freq_mhz=[500,450,400],preview_freq_mhz=[500,450,400],t0=1.,t1=2.,integration_s=1.,preview_times=[1.,2.])
    (dest/'inspection.json').write_text(json.dumps(meta))
    (dest.parent/'result.json').write_text('{}')
    ones=np.ones((2,3))
    weights=np.exp(1j*np.array([[.4,.5,.6],[.7,.8,.9]]))
    np.savez(dest/'antennas.npz',phase_weights=weights,weights=weights,reference_weights=weights,
             amp_uniform=ones,auto=ones,night=ones,static_auto=ones,residual_rms=ones*.2)
    z=np.ones(3,dtype=complex)*1j
    np.savez(dest/'pair_9_12.npz',**{f'{stage}_{suffix}':value for stage in meta['stages']
        for suffix,value in [('mean',z),('amp',abs(z)),('dynamic',np.tile(z,(2,1))),('dynamic_amp',np.ones((2,3)))]})
    return tmp_path


def test_reference_conjugation_and_weight_difference(saved):
    first=snapshot(saved,9,quantity='phase')
    np.testing.assert_allclose(first['panels'][0]['values'],-np.pi/2)
    second=snapshot(saved,12,quantity='phase')
    np.testing.assert_allclose(second['panels'][0]['values'],np.pi/2)
    for panel in first['antenna_panels']:
        np.testing.assert_allclose(panel['phase_difference'],0,atol=1e-15)
    assert first['panels'][0]['tile']['src'].startswith('data:image/png;base64,')


def test_reject_invalid_selections(saved):
    for params in ({'reference':99},{'target':9},{'stage':'delay'},{'scheme':'secret'},
                   {'stage':'geometric','quantity':'amp'}):
        with pytest.raises(ValueError):snapshot(saved,**params)
    with pytest.raises(ValueError):latest_inspection(saved/'nonexistent')


def test_saved_calibration_waterfall_keeps_restart_gaps(saved):
    import base64
    import io
    from PIL import Image
    path = saved/'actions'/'test'/'inspection'/'inspection.json'
    meta = json.loads(path.read_text())
    meta.update(preview_times=[1., 10.25], t1=10.25)
    path.write_text(json.dumps(meta))
    tile = snapshot(saved)['panels'][0]['tile']
    pixels = np.asarray(Image.open(io.BytesIO(base64.b64decode(tile['src'].split(',')[1]))))
    assert tile['time_start'] == .5 and tile['time_end'] == 10.75
    assert np.all(pixels[:, tile['width']//2] == [238,240,243,255])
    assert not np.any(np.all(pixels[:,0] == [238,240,243,255], axis=1))
    del meta['preview_times']
    path.write_text(json.dumps(meta))
    with pytest.raises(ValueError, match='timestamps are missing'):
        snapshot(saved)


def test_incomplete_inspection_is_not_published(saved):
    (saved/'actions'/'test'/'result.json').unlink()
    with pytest.raises(ValueError,match='Prepare'):
        latest_inspection(saved)


def test_incompatible_static_diagnostic_is_not_reused(saved):
    path=saved/'actions'/'test'/'inspection'/'inspection.json'
    meta=json.loads(path.read_text());meta['static_used']=True
    path.write_text(json.dumps(meta))
    assert latest_inspection(saved,True)[1]['static_used']
    with pytest.raises(ValueError,match='Prepare'):
        latest_inspection(saved,False)


def test_auto_template_is_the_subtracted_mean_not_the_noise_median(saved):
    path=saved/'actions'/'test'/'inspection'/'antennas.npz'
    with np.load(path) as src:
        arrays=dict(src)
    arrays.update(auto=np.array([[5.,9.,3.],[7.,2.,4.]]),
                  static_auto=np.array([[2.,12.,1.],[3.,4.,np.nan]]),
                  night=np.ones((2,3))*99)
    np.savez(path,**arrays)
    first=snapshot(saved)['antenna_panels'][0]
    assert first['auto']==[5.,9.,3.]
    assert first['static']==[2.,12.,1.]
    assert first['subtracted']==[3.,-3.,2.]
    assert first['night']==[99.,99.,99.]
    second=snapshot(saved)['antenna_panels'][1]
    assert second['static'][-1] is None and second['subtracted'][-1] is None


def test_inspect_validation_no_unbounded_window():
    from datetime import datetime,timedelta,timezone
    day=(datetime.now(timezone.utc)-timedelta(days=2)).strftime('%Y-%m-%d')
    record={'params':{'source_window':[day+' 18:00:00',day+' 19:00:00']}}
    result=action_params({'action':'inspect'},record)
    assert not result['static'] and result['source']=='sun'
    assert action_params(result,record)==result
    with pytest.raises(ValueError,match='four hours'):
        action_params({'action':'inspect','window':[day+' 10:00:00',day+' 15:00:00']},record)
