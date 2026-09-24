"""Explicit, bounded native-recording reads through casm_io, never fallback."""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

import numpy as np
from fastapi import HTTPException

from ..observation import file_identity


def recorded_plan(settings,req,t0,t1):
    from casm_io.correlator.reader import discover_observations, discover_files
    if not 0 < t1-t0 <= 3600:
        raise HTTPException(400,'Native recording reads require at most one hour per interval')
    from ..collectors.vis import DT_S
    if t1 > time.time()-2*DT_S:
        raise HTTPException(400,'Native recording end must be at least two integrations behind now to avoid an accumulating write')
    # Only configured correlator directory, never recursive /mnt discovery.
    matches=[o for o in discover_observations(str(settings.vis_dir)) if o['time_start']<t1 and o['time_end']>t0]
    if not matches or len(matches)>4:
        raise HTTPException(404,'Need 1..4 overlapping observations in the configured recording directory')
    inputs=sorted({i for p in req.pairs for i in p})
    nbl=len(inputs)*(len(inputs)+1)//2
    total=0
    evidence=[]
    for obs in matches:
        fmt=obs['fmt']
        if fmt.nchan!=3072 or fmt.dt_raw_s<100:
            raise HTTPException(400,'Unsupported recording geometry for bounded monitoring read')
        samples=math.ceil((min(t1,obs['time_end'])-max(t0,obs['time_start']))/fmt.dt_raw_s)+2
        total+=samples*fmt.nchan*nbl*8
        for part,path in discover_files(str(settings.vis_dir),obs['base_str']).items():
            a=obs['time_start']+part*fmt.file_duration_s
            b=a+fmt.file_duration_s
            if a<t1 and b>t0:
                identity=file_identity(path)
                evidence.append({'id':f"{identity['path']}:{identity['size']}:{identity['mtime_ns']}",**identity,
                                 't0':a,'t1':b,'obs':obs['base_str']})
    if total>64*1024**2:
        raise HTTPException(400,'Native recording selected triangle exceeds 64 MiB; shorten interval or select fewer inputs')
    return matches,evidence


def read_recorded(settings,req,t0,t1):
    from casm_io.correlator import read_visibilities
    from casm_io.correlator.baselines import triu_flat_index
    _,evidence=recorded_plan(settings,req,t0,t1)
    inputs=sorted({i for p in req.pairs for i in p})
    try:
        result=read_visibilities(datetime.fromtimestamp(t0,timezone.utc),datetime.fromtimestamp(t1,timezone.utc),
                                 time_tz='UTC',data_dir=str(settings.vis_dir),inputs=inputs,
                                 freq_order='descending',freq_range_mhz=(req.fmin,req.fmax),workers=1,verbose=False)
    except (ValueError,RuntimeError,OverflowError,OSError) as exc:
        raise HTTPException(409,f'Canonical native recording read failed: {exc}. Try a narrower interval.') from exc
    meta=result.metadata
    if list(meta.get('inputs',inputs))!=inputs:
        raise HTTPException(409,'Reader subset mapping differs from requested inputs')
    rank={p:k for k,p in enumerate(inputs)}
    flat=[triu_flat_index(len(inputs),rank[i],rank[j]) for i,j in req.pairs]
    z=np.swapaxes(np.asarray(result.vis)[:,:,flat],1,2)
    stamps=np.asarray(result.time_unix)
    keep=(stamps>=t0)&(stamps<=t1)
    # Retain recorded integrations at file boundaries as well.
    # casm_io represents wholly missing files with zeros. Preserve as absent.
    bad=np.all(z==0,axis=(1,2))
    omitted=int(np.count_nonzero(keep&bad))
    keep&=~bad
    return z[keep],stamps[keep],np.asarray(result.freq_mhz),evidence,omitted
