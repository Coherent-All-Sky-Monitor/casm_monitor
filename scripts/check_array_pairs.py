"""Check ordered dynamic-spectrum batches against read-only complex cache data."""
import base64
import io
import json
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
from matplotlib import colormaps
from PIL import Image

from casm_monitor import vis_ops
from casm_monitor.collectors.vis import STREAM_AVG8
from casm_monitor.config import load_settings
from casm_monitor.store import Store
from casm_monitor.web.vis import VisStore, input_positions
from check_array_science import request


def main():
    settings=load_settings()
    store=VisStore(settings, Store(settings.db_path, read_only=True))
    data,_=request('/api/science/array')
    pairs=[(31, 8), (8, 9), (18, 18)]
    z,t,f,_=store.series(STREAM_AVG8, data['t0'], data['t1'], [tuple(sorted(p)) for p in pairs], allow_full_fallback=False)
    mask=(f>=min(data['freq_mhz']))&(f<=max(data['freq_mhz']))
    z,f=z[:,:,mask],f[mask]
    report=dict(t0=data['t0'], t1=data['t1'], samples=len(t), checks=[], result='passed')
    for reference in ('raw', 'sun'):
        values=z.copy()
        if reference=='sun':
            pos=input_positions(data['provenance']['layout']['path'])
            ids=sorted({i for pair in pairs for i in pair}); rank={p:k for k,p in enumerate(ids)}
            values=vis_ops.fringe_stop_sun(values, f, np.array([pos[i] for i in ids]),
                [tuple(rank[i] for i in sorted(p)) for p in pairs], t)
        for k,(i,j) in enumerate(pairs):
            if i>j: values[:,k]=values[:,k].conj()
        # Independent frequency grouping. Never time-average, nor average angles.
        block=int(np.ceil(len(f)/128))
        mean=np.stack([np.nanmean(values[:,:,k:k+block], axis=-1, dtype=np.complex128) for k in range(0,len(f),block)],axis=-1)
        magnitude=np.stack([np.nanmean(np.abs(values[:,:,k:k+block]), axis=-1, dtype=np.float64) for k in range(0,len(f),block)],axis=-1)
        quantities=dict(real=mean.real, imag=mean.imag, amp=magnitude,
                        phase=np.where(np.abs(mean)>0, np.angle(mean), np.nan))
        for q,v in quantities.items():
            params=dict(pairs=','.join(f'{i}:{j}' for i,j in pairs),t0=t[0],t1=t[-1],
                        fmin=min(f), fmax=max(f), quantity=q, reference=reference)
            result,elapsed=request('/api/science/array/pairs?'+urlencode(params))
            assert result['samples']==len(t) and result['quantity']==q
            for k,panel in enumerate(result['panels']):
                assert panel['pair']==list(pairs[k])
                tile=panel['tile'];a=v[:,k];finite=a[np.isfinite(a)]
                if q=='phase': lo,hi,cmap=-np.pi,np.pi,'twilight_shifted'
                elif q=='amp':
                    lo,hi=np.percentile(finite[finite>0],[2,98]);hi=max(hi,lo*1.01);cmap='magma'
                else:
                    hi=max(np.percentile(np.abs(finite),98),1e-12);lo=-hi;cmap='RdBu_r'
                np.testing.assert_allclose([tile['min'],tile['max']],[lo,hi],rtol=2e-7)
                scaled=(np.log10(np.maximum(a,lo))-np.log10(lo))/(np.log10(hi)-np.log10(lo)) if q=='amp' else (a-lo)/(hi-lo)
                rgb=colormaps[cmap](np.clip(np.nan_to_num(scaled),0,1),bytes=True)[...,:3]
                rgb[~np.isfinite(a)]=(29,35,43)
                ix=np.rint((t-t[0])/data['integration_s']).astype(int)
                expected=np.full((ix[-1]+1,a.shape[1],3),(29,35,43),dtype=np.uint8);expected[ix]=rgb
                actual=np.array(Image.open(io.BytesIO(base64.b64decode(tile['src'].split(',')[1]))))
                np.testing.assert_array_equal(actual,expected.swapaxes(0,1))
            report['checks'].append(dict(reference=reference,quantity=q,pairs=pairs,seconds=elapsed))
            print(f'{reference} {q}: all raster pixels match',flush=True)
    target=Path('/home/casm/scratch/casm-observation-preview/array-pairs-audit.json')
    target.write_text(json.dumps(report,indent=2)+'\n')
    print(target)


if __name__=='__main__': main()
