"""Bounded live array overview, reusing the visibility cache and reference transform.

Small raster previews and full cached-channel spectra travel together, so changing
quantity needs no new data read. No observing, acquisition or persistent writes.
"""
from __future__ import annotations

import base64
import gzip
import io
import json
import threading
from collections import OrderedDict
from types import SimpleNamespace

import numpy as np
from fastapi import HTTPException, Response

from .. import vis_ops
from ..collectors.vis import DT_S, STREAM_AVG8
from .vis import VisStore

# Operator's inspection set. Deliberately independent of beam deployment/gating.
INSPECTION_ANTENNAS = (9, 10, 15, 18, 19, 22, 23, 24, 26, 30, 32, 36, 38, 40, 42, 44, 45)
_CACHE: OrderedDict[tuple, bytes] = OrderedDict()
_LOCK = threading.Lock()


def phase(values):
    values = np.asarray(values)
    return np.where(np.isfinite(values) & (np.abs(values) > 0), np.angle(values), np.nan)


def components(values, axis=None):
    """Magnitude means are scalar; phase is always the angle of a complex mean."""
    z = np.asarray(values)
    mean = z if axis is None else vis_ops._nanmean(z, axis=axis, dtype=np.complex128)
    amp = np.abs(z) if axis is None else vis_ops._nanmean(np.abs(z), axis=axis, dtype=np.float64)
    return dict(real=mean.real, imag=mean.imag, amp=amp, phase=phase(mean))


def finite_list(values):
    a = np.asarray(values)
    return np.where(np.isfinite(a), a, None).tolist()


def preview_values(z, max_channels=128):
    """Frequency-only preview reduction; preserve every recorded integration."""
    mean = vis_ops.block_mean(z, max_channels)
    return dict(real=mean.real, imag=mean.imag,
                amp=vis_ops.block_mean(np.abs(z), max_channels), phase=phase(mean))


def image_tile(values, stamps, quantity):
    from matplotlib import colormaps
    from PIL import Image
    a = np.asarray(values, dtype=float)
    finite = a[np.isfinite(a)]
    if quantity == 'phase':
        lo, hi, cmap, logarithmic = -np.pi, np.pi, 'twilight_shifted', False
    elif quantity == 'amp':
        positive = finite[finite > 0]
        lo, hi = np.percentile(positive, [2, 98]) if len(positive) else (1., 10.)
        hi = max(float(hi), float(lo) * 1.01)
        cmap, logarithmic = 'magma', True
    else:
        limit = float(np.percentile(np.abs(finite), 98)) if len(finite) else 1.
        limit = max(limit, 1e-12)
        lo, hi, cmap, logarithmic = -limit, limit, 'RdBu_r', False
    if logarithmic:
        scaled = (np.log10(np.maximum(a, lo)) - np.log10(lo)) / (np.log10(hi) - np.log10(lo))
    else:
        scaled = (a - lo) / (hi - lo)
    rgb = colormaps[cmap](np.clip(np.nan_to_num(scaled), 0, 1), bytes=True)[..., :3]
    rgb[~np.isfinite(a)] = (29, 35, 43)
    # Keep empty integrations empty. Do not stretch adjacent measurements over gaps.
    ix = np.rint((stamps - stamps[0]) / DT_S).astype(int)
    if np.any(np.abs((stamps-stamps[0])/DT_S - ix) > .05) or len(set(ix)) != len(ix):
        raise HTTPException(409, 'Irregular timestamps require the detailed visibility view')
    pixels = np.full((ix[-1]+1, a.shape[1], 3), (29,35,43), dtype=np.uint8)
    pixels[ix] = rgb
    image = Image.fromarray(np.swapaxes(pixels, 0, 1))
    buf = io.BytesIO()
    image.save(buf, format='PNG')
    return dict(src='data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode(),
                min=float(lo), max=float(hi), log=logarithmic,
                width=image.width, height=image.height)


def make_panels(z, stamps, freq, inputs, pairs, reference_input, mode):
    previews = preview_values(z)
    latest, averaged = components(z[-1]), components(z, axis=0)
    panels = []
    for k, entry in enumerate(inputs):
        p = entry['packet_idx']
        # All cross tiles represent V(target, reference), regardless of packet order.
        conjugate = mode == 'cross' and p > reference_input
        spectra = {}
        images = {}
        for q in ('real', 'imag', 'amp', 'phase'):
            sign = -1 if conjugate and q in ('imag','phase') else 1
            spectra[q] = dict(latest=finite_list(sign*latest[q][k]), mean=finite_list(sign*averaged[q][k]))
            images[q] = image_tile(sign*previews[q][:,k], stamps, q)
        panels.append(dict(input=p, pair=list(pairs[k]), is_auto=pairs[k][0]==pairs[k][1],
                           spectra=spectra, images=images,
                           valid_fraction=float(np.mean(np.isfinite(z[:,k])))))
    return panels


def matrix_values(z, pairs, inputs):
    """Latest integration, selected-band summary; lower triangle is conjugate."""
    rank = {entry['packet_idx']:k for k,entry in enumerate(inputs)}
    values = components(z, axis=-1)
    result = {}
    for q, data in values.items():
        matrix = np.full((len(inputs),len(inputs)), np.nan)
        for value, (i,j) in zip(data, pairs):
            a,b = rank[i],rank[j]
            matrix[a,b] = value
            matrix[b,a] = -value if q in ('imag','phase') and a != b else value
        result[q] = finite_list(matrix)
    return result


def snapshot(settings, reader, *, hours=24., reference_input=8, mode='auto', reference='raw',
             fmin=390.625, fmax=484.375):
    from .science import geometry, select_layout, bounded_rows, load_selection
    if mode not in ('auto','cross') or reference not in ('raw','sun'):
        raise HTTPException(400, 'Unknown visibility mode or reference')
    if not np.isfinite([hours,fmin,fmax]).all() or not .1 <= hours <= 24 or not 0 < fmin < fmax < 1000:
        raise HTTPException(400, 'Choose 0.1–24 hours and increasing frequency bounds')
    latest = reader.query('SELECT MAX(t1) AS t FROM shards WHERE stream=?', (STREAM_AVG8,))
    if not latest or latest[0]['t'] is None:
        raise HTTPException(404, 'No averaged visibility cache available')
    t1 = float(latest[0]['t'])
    t0 = t1-hours*3600
    layout = select_layout(t0,t1)
    inputs,_ = geometry(layout['path'])
    if any(i['position_enu_m'] is None for i in inputs):
        raise HTTPException(409, 'Wired antenna has no position in the dated layout')
    inputs.sort(key=lambda a:(-a['position_enu_m'][1],a['position_enu_m'][0]))
    if not 1 <= len(inputs) <= 32 or reference_input not in {i['packet_idx'] for i in inputs}:
        raise HTTPException(400, 'Reference must be a wired antenna in the selected layout')
    pairs = [(i['packet_idx'],i['packet_idx']) if mode=='auto' else tuple(sorted((i['packet_idx'],reference_input))) for i in inputs]
    vstore = VisStore(settings,reader)
    rows = bounded_rows(vstore,STREAM_AVG8,t0,t1,pairs)
    layout_identity = tuple((i['packet_idx'],i['antenna'],i['station'],*i['position_enu_m']) for i in inputs)
    key = (str(settings.store_root),layout['id'],layout_identity,reference_input,mode,reference,hours,fmin,fmax,
           tuple((r['id'],r['t1']) for r in rows))
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
        req = SimpleNamespace(pairs=pairs,reference=reference,resolution='avg8',fmin=fmin,fmax=fmax)
        z,t,f,evidence,_ = load_selection(vstore,req,t0,t1,layout)
        panels = make_panels(z,t,f,inputs,pairs,reference_input,mode)
        # All-pairs summary uses only the newest integration, not the history cube.
        ids = sorted(i['packet_idx'] for i in inputs)
        matrix_pairs = [(i,j) for k,i in enumerate(ids) for j in ids[k:]]
        # load_selection needs two integrations; series directly for the one-sample matrix.
        mz,mt,mf,_ = vstore.series(STREAM_AVG8,t[-1],t[-1],matrix_pairs,allow_full_fallback=False)
        if len(mt) != 1 or mt[0] != t[-1]:
            raise HTTPException(409, 'Latest integration changed during read; refresh the snapshot')
        mask = (mf>=fmin)&(mf<=fmax)
        mz = mz[:,:,mask]
        if reference == 'sun':
            from .vis import input_positions
            pos=input_positions(layout['path']); rank={p:k for k,p in enumerate(ids)}
            mz=vis_ops.fringe_stop_sun(mz,mf[mask],np.array([pos[p] for p in ids]),
                                     [(rank[i],rank[j]) for i,j in matrix_pairs],mt)
        result = dict(inputs=inputs,default_inputs=[i['packet_idx'] for i in inputs if i['antenna'] in INSPECTION_ANTENNAS],
                      reference_input=reference_input,mode=mode,reference=reference,panels=panels,
                      freq_mhz=f.tolist(),t0=float(t[0]),t1=float(t[-1]),samples=len(t),integration_s=DT_S,
                      matrix=matrix_values(mz[-1],matrix_pairs,inputs),
                      selection=dict(hours=hours,reference_input=reference_input,mode=mode,reference=reference,fmin=fmin,fmax=fmax),
                      provenance=dict(layout=layout,stream=STREAM_AVG8,source_shards=evidence,
                                      spectrum='Latest integration or explicitly selected window mean',
                                      amplitude='Mean magnitude; no channel-mean normalization',
                                      phase='Angle of complex value/mean; exact zero is undefined',
                                      previews='Frequency-only reduction to at most 128 channels; every time integration retained',
                                      baseline_convention='V(target, reference); reversed stored pairs are conjugated',
                      membership='Operator inspection preset, independent of beam deployment'))
        body = gzip.compress(json.dumps(result,allow_nan=False,separators=(',',':')).encode(),compresslevel=3)
        _CACHE[key] = body
        while len(_CACHE)>4:
            _CACHE.popitem(last=False)
        return body


def register_routes(router,settings,reader):
    @router.get('/array')
    def array(hours:float=24, reference_input:int=8, mode:str='auto', reference:str='raw',
              fmin:float=390.625, fmax:float=484.375):
        body=snapshot(settings,reader,hours=hours,reference_input=reference_input,mode=mode,
                      reference=reference,fmin=fmin,fmax=fmax)
        return Response(body,media_type='application/json',headers={'Content-Encoding':'gzip','Cache-Control':'no-cache'})
