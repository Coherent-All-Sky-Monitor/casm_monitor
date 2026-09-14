from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import HTTPException

from casm_monitor.web.science import ScienceRequest
from casm_monitor.web.science_recorded import recorded_plan, read_recorded


def fixture(monkeypatch,tmp_path):
    import casm_io.correlator.reader as r
    fmt=SimpleNamespace(nchan=3072,dt_raw_s=137.438953472,ntime_per_file=32,file_duration_s=4398.046511104)
    p=tmp_path/'data.part'; p.write_bytes(b'fixture')
    obs=dict(base_str='fixture',time_start=0,time_end=4398.046511104,fmt=fmt)
    monkeypatch.setattr(r,'discover_observations',lambda *a:[obs])
    monkeypatch.setattr(r,'discover_files',lambda *a:{0:str(p)})
    return fmt


def test_recorded_bounds_before_reader(monkeypatch,tmp_path,settings):
    fixture(monkeypatch,tmp_path)
    req=ScienceRequest(pairs=[(i,i+12) for i in range(6)],t0=0,t1=3601,resolution='recorded')
    with pytest.raises(HTTPException):
        recorded_plan(settings,req,0,3601)


def test_recorded_refuses_current_accumulating_interval(monkeypatch,settings):
    import casm_monitor.web.science_recorded as module
    monkeypatch.setattr(module.time,'time',lambda:1000)
    req=ScienceRequest(pairs=[(8,18)],t0=500,t1=950,resolution='recorded')
    with pytest.raises(HTTPException) as caught:
        recorded_plan(settings,req,500,950)
    assert 'accumulating' in caught.value.detail


def test_recorded_native_mapping_and_flags(monkeypatch,tmp_path,settings):
    import casm_io.correlator
    fmt=fixture(monkeypatch,tmp_path)
    req=ScienceRequest(pairs=[(8,18)],t0=0,t1=500,resolution='recorded')
    def read(*args,**kw):
        assert kw['inputs']==[8,18] and kw['workers']==1
        assert kw['data_dir']==str(settings.vis_dir)
        assert 'data_root' not in kw
        z=np.ones((4,3,3),np.complex64)
        z[:,:,1]=2+3j
        return SimpleNamespace(vis=z,time_unix=np.arange(4)*fmt.dt_raw_s,freq_mhz=np.array([430,420,410]),metadata={})
    monkeypatch.setattr(casm_io.correlator,'read_visibilities',read)
    z,t,f,ev,omitted=read_recorded(settings,req,0,500)
    assert z.shape==(3,1,3) and np.all(z==2+3j)
    assert omitted==1 and ev[0]['size']==7
