from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException

from casm_io.correlator.mapping import AntennaMapping
from casm_monitor.web import science_transit as transit


def mapping():
    return AntennaMapping(pd.DataFrame(dict(antenna_id=[9,19],snap_id=[0,1],adc=[8,6],packet_index=[8,18],
                                          x_m=[0.,0.],y_m=[0.,10.],z_m=[0.,0.],functional=[1,1],include_in_beamforming=[1,1])))


def test_compact_triangle_preserves_physical_ids_and_selected_reads(monkeypatch,settings,store):
    m=mapping()
    rows=[dict(id=1,t0=0,t1=274,meta=dict(t=[0,137,274],nchan=3072,freq_top_mhz=484.375,chan_bw_mhz=.030517578125))]
    def series(stream,t0,t1,pairs,**kw):
        assert pairs == [(8,8),(8,18),(18,18)]
        assert kw == {'allow_full_fallback':False}
        return np.ones((3,3,3072),np.complex64),np.array([0,137,274]),484.375-np.arange(3072)*.030517578125,[8,18]
    monkeypatch.setattr(transit,'VisStore',lambda *a:SimpleNamespace(shards=SimpleNamespace(list=lambda *a,**k:rows),series=series))
    data,compact,_,size=transit.native_inputs(store,m,[9,19],0,274,settings)
    assert compact.packet_index(9)==0 and compact.packet_index(19)==1
    assert compact.active_antennas()==[9,19]
    assert m.packet_index(9)==8
    assert data['vis'].shape==(3,3072,3)
    assert size==3*3072*3*8


def test_transit_no_cache_and_budget_refused(monkeypatch,settings,store):
    monkeypatch.setattr(transit,'VisStore',lambda *a:SimpleNamespace(shards=SimpleNamespace(list=lambda *a,**k:[])))
    with pytest.raises(HTTPException) as e:
        transit.native_inputs(store,mapping(),[9,19],0,1000,settings)
    assert e.value.status_code==404


def test_transit_canonical_beam_and_model_output(monkeypatch,settings,store,tmp_path):
    import bf_weights_generator
    import bf_weights_generator.plot_transit
    from astropy.time import Time
    from bf_weights_generator import CalibrationWeights
    settings=replace(settings,observation_cache_root=tmp_path/'artifacts')
    p=tmp_path/'fixture.csv'
    p.write_text('fixture')
    monkeypatch.setattr(transit,'calibration_catalog',lambda s:[dict(id='fixture',path='cal.h5',size=100)])
    monkeypatch.setattr(transit,'select_layout',lambda *a:dict(path=str(p),id='fixture'))
    monkeypatch.setattr(AntennaMapping,'load',lambda *a:mapping())
    f=484.375-np.arange(3072)*.030517578125
    cal=CalibrationWeights(weights=np.ones((2,3072),complex),flags=np.ones(3072,bool),frequencies_hz=f*1e6,ant_ids=np.array([9,19]),ref_ant_id=9)
    monkeypatch.setattr(bf_weights_generator,'load_calibration_weights',lambda p:cal)
    m=mapping()
    df=m.dataframe.copy(); df['packet_index']=[0,1]
    compact=AntennaMapping(df)
    stamps=1789344000+np.arange(3)*137
    data=dict(vis=np.ones((3,3072,3),np.complex64),freq_mhz=f,time_unix=stamps)
    monkeypatch.setattr(transit,'native_inputs',lambda *a:(data,compact,[dict(id=1)],300000))
    def exact(path,*a,**kw):
        import h5py
        with h5py.File(path) as h:
            assert 'weights_int8' not in h
            assert h['array_config/positions_enu'].shape==(2,3)
        return dict(times=Time(stamps,format='unix'),response=np.ones((2,3)))
    monkeypatch.setattr(bf_weights_generator.plot_transit,'array_factor_response',exact)
    req=transit.TransitRequest(calibration_id='fixture',t0=float(stamps[0]),t1=float(stamps[-1]),fixed_alt_deg=86,
                              fixed_az_deg=0,control_alt_deg=45,control_az_deg=90)
    result=transit.render_transit(settings,store,req)
    root=transit.cache_dir(settings)/'science'/result['id']
    assert (root/'plot-0.png').stat().st_size>1000
    saved=np.load(root/'data.npz')
    assert saved['power'].shape==(3,) and saved['exact_response'].shape==(2,3)
    assert result['provenance']['background']=='none'
