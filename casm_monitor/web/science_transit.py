"""Requested stationary Cyg A check through canonical beam and exact-model APIs.

Only native cached visibilities are read. Geometry-only model files are local
render artifacts, not usable beamforming/calibration products.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone

import numpy as np
from fastapi import HTTPException
from pydantic import BaseModel, field_validator

from ..observation import file_identity, cache_dir
from ..collectors.vis import STREAM_FULL
from .vis import VisStore, deployed_cal_path, _shard_meta_times
from .science import ScienceRequest, PLOT_LOCK, select_layout, code_provenance


class TransitRequest(BaseModel):
    calibration_id: str
    recorded_product_id: str | None = None
    t0: float
    t1: float
    fixed_alt_deg: float
    fixed_az_deg: float
    control_alt_deg: float
    control_az_deg: float
    fmin: float = 410
    fmax: float = 470

    @field_validator('t0', 't1', mode='before')
    @classmethod
    def parse_time(cls, value):
        return ScienceRequest.parse_time(value)


def calibration_catalog(settings):
    try:
        path = deployed_cal_path(settings)
        identity = file_identity(path)
        cid = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
        return [{'id': cid, **identity, 'association': 'Latest deployment ledger calibration; mixed-cal beam products may use other solutions.'}]
    except (HTTPException, OSError):
        return []


def native_inputs(reader, mapping, ant_ids, t0, t1, settings):
    """Reindex a selected triangle and mapping together, without a padded 128² cube."""
    from casm_io.correlator.mapping import AntennaMapping
    vs = VisStore(settings, reader)
    rows = vs.shards.list(STREAM_FULL, t0=t0, t1=t1)
    if not rows:
        raise HTTPException(404, 'No native visibility cache for this transit; no avg8 or raw fallback')
    inputs = sorted(mapping.packet_index(a) for a in ant_ids)
    pairs = [(a,b) for n,a in enumerate(inputs) for b in inputs[n:]]
    samples = sum(len(_shard_meta_times(r)) for r in rows)
    nchan = {int((r.get('meta') or {}).get('nchan', 0)) for r in rows}
    axes = {((r.get('meta') or {}).get('freq_top_mhz'), (r.get('meta') or {}).get('chan_bw_mhz')) for r in rows}
    if nchan != {3072} or len(axes) != 1 or samples > 55:
        raise HTTPException(400, 'Transit requires consistent 3072-channel native cache and at most 55 integrations')
    read_bytes = samples * 3072 * len(pairs) * 8
    if read_bytes > 500 * 1024**2:
        raise HTTPException(400, 'Native selected-matrix read exceeds 500 MiB; shorten the interval')
    z, stamps, freq, _ = vs.series(STREAM_FULL, t0, t1, pairs, allow_full_fallback=False)
    bad = set()
    for row in rows:
        meta = row.get('meta') or {}
        if (meta.get('flags') or {}).get('first_of_file'):
            bad.update(_shard_meta_times(row))
    keep = np.array([t not in bad for t in stamps]) & np.isfinite(z).all(axis=(1,2))
    z, stamps = z[keep], stamps[keep]
    if len(stamps) < 3 or np.any(np.diff(stamps) <= 0):
        raise HTTPException(404, 'Need three finite, distinct native integrations containing every selected antenna')
    df = mapping.dataframe.copy()
    df = df[df['antenna_id'].isin(ant_ids)].copy()
    ranks = {p:n for n,p in enumerate(inputs)}
    df['packet_index'] = [ranks[int(p)] for p in df['packet_index']]
    df['functional'] = 1
    df['include_in_beamforming'] = 1
    compact = AntennaMapping(df)
    return {'vis': np.swapaxes(z,1,2), 'time_unix': stamps, 'freq_mhz': freq}, compact, rows, read_bytes


def render_transit(settings, reader, req):
    from casm_io.correlator.mapping import AntennaMapping
    from bf_weights_generator import load_calibration_weights
    from bf_weights_generator.plot_transit import array_factor_response
    from casm_vis_analysis.beam_power import beam_power_vs_time, plot_beam_power
    vals = [req.t0,req.t1,req.fmin,req.fmax,req.fixed_alt_deg,req.fixed_az_deg,req.control_alt_deg,req.control_az_deg]
    if not np.isfinite(vals).all() or not 0 < req.t1-req.t0 <= 7200 or not 0 < req.fmin < req.fmax < 1000:
        raise HTTPException(400, 'Use finite bounds and a positive interval of at most two hours')
    if not all(0 <= a <= 90 for a in [req.fixed_alt_deg,req.control_alt_deg]) or not all(0 <= a < 360 for a in [req.fixed_az_deg,req.control_az_deg]):
        raise HTTPException(400, 'Pointings require altitude 0..90 and azimuth 0..<360 degrees')
    if (req.fixed_alt_deg,req.fixed_az_deg) == (req.control_alt_deg,req.control_az_deg):
        raise HTTPException(400, 'Select a distinct fixed off-source control direction')
    start, end = [datetime.fromtimestamp(t,timezone.utc) for t in (req.t0,req.t1)]
    if start.date() != end.date():
        raise HTTPException(400, 'Exact-model API requires this bounded view within one UTC date')
    item = next((c for c in calibration_catalog(settings) if c['id'] == req.calibration_id), None)
    if not item:
        raise HTTPException(409, 'Calibration identity unavailable or changed; reload catalog')
    if req.recorded_product_id:
        from ..collectors.weights import load_product
        import re
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',req.recorded_product_id) or not load_product(settings.registry_dir,req.recorded_product_id):
            raise HTTPException(400,'Unknown recorded product context')
    if item['size'] > 32 * 1024**2:
        raise HTTPException(400, 'Calibration metadata product exceeds 32 MiB budget')
    layout = select_layout(req.t0,req.t1)
    mapping = AntennaMapping.load(layout['path'])
    cal = load_calibration_weights(item['path'])
    wired = set(mapping.dataframe.loc[mapping.dataframe.functional == 1,'antenna_id'].astype(int))
    ants = sorted(set(map(int,cal.ant_ids)))
    if not 2 <= len(ants) <= 32 or not set(ants) <= wired:
        raise HTTPException(409, 'Calibration requires 2..32 antennas, all wired in the dated layout; no silent subset')
    if not PLOT_LOCK.acquire(blocking=False):
        raise HTTPException(429, 'Another scientific view is rendering')
    try:
        data, compact, rows, read_bytes = native_inputs(reader,mapping,ants,req.t0,req.t1,settings)
        freq = data['freq_mhz']
        cf = np.asarray(cal.frequencies_hz)/1e6
        flags = np.asarray(cal.flags,dtype=bool)
        if cf[0] < cf[-1]:
            cf, flags = cf[::-1], flags[::-1]
        if cf.shape != freq.shape or not np.allclose(cf,freq,atol=1e-5,rtol=0):
            raise HTTPException(409, 'Calibration frequency centers do not exactly match native cache')
        if np.count_nonzero((freq>=req.fmin)&(freq<=req.fmax)&flags)<2:
            raise HTTPException(400,'Select a band containing at least two good calibration channels')
        data['freq_mask'] = ~flags
        selection = req.model_dump()
        identity = {'version':1, 'code':code_provenance(), 'kind':'cyga_transit', 'selection':selection, 'calibration':item,
                    'layout':file_identity(layout['path']), 'shards':[r.get('id') for r in rows]}
        pid = hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:32]
        dest = cache_dir(settings)/'science'/pid
        if dest.is_symlink() or dest.resolve().parent != (cache_dir(settings)/'science').resolve():
            raise HTTPException(400,'Unsafe scientific artifact directory')
        dest.mkdir(parents=True,exist_ok=True)
        directions = [('Fixed Cyg A beam',req.fixed_alt_deg,req.fixed_az_deg),
                      ('Fixed off-source control',req.control_alt_deg,req.control_az_deg)]
        result = beam_power_vs_time(data,compact,sources=directions,cal_weights=cal,
                                    freq_band_mhz=(req.fmin,req.fmax),sign=-1)
        # A geometry-only fixture feeds the maintained exact model. This is NOT
        # a weights/calibration build and has no payload usable by the telescope.
        import h5py
        model_path = dest/'model_geometry.h5'
        positions = compact.dataframe[['x_m','y_m','z_m']].to_numpy()
        with h5py.File(model_path,'w') as h:
            h['pointings/alt_deg'] = [req.fixed_alt_deg,req.control_alt_deg]
            h['pointings/az_deg'] = [req.fixed_az_deg,req.control_az_deg]
            h['array_config/positions_enu'] = positions
            h['array_config/active_mask'] = np.ones(len(positions),dtype=bool)
            h['frequencies_hz'] = freq[(freq >= req.fmin)&(freq <= req.fmax)&flags]*1e6
        exact = array_factor_response(model_path,'cyg-a',date=start.strftime('%Y-%m-%d'),time_tz='UTC',
                                      time_start=start.strftime('%H:%M'),time_end=end.strftime('%H:%M'),
                                      dt_minutes=2,n_freq=32)
        import matplotlib.pyplot as plt
        with plt.style.context('dark_background'):
            fig = plot_beam_power(result,time_tz='UTC')
            ax = fig.axes[0]
            ax.legend(loc='upper left',fontsize=7)
            model_ax = ax.twinx()
            # Keep the exact normalized model on its OWN scale: no fitted
            # amplitude/offset can silently make a poor observed curve agree.
            for k,label in enumerate(['Cyg A exact ideal array factor','Control exact ideal array factor']):
                model_ax.plot(exact['times'].to_datetime(timezone=timezone.utc),exact['response'][k],linestyle='--',alpha=.7,label=label)
            model_ax.set_ylabel('Ideal equal-amplitude array response (0–1)')
            model_ax.set_ylim(0,1.05)
            model_ax.legend(loc='upper right',fontsize=7)
            fig.savefig(dest/'plot-0.png',dpi=130,bbox_inches='tight',facecolor='#000000')
            plt.close(fig)
        np.savez_compressed(dest/'data.npz',time_unix=result['time_unix'],power=result['power'][directions[0][0]],
                            control_power=result['power'][directions[1][0]],model_time_unix=exact['times'].unix,
                            exact_response=exact['response'],freq_mhz=freq,antenna_ids=np.asarray(ants))
        output = {'id':pid,'images':[{'url':f'/api/science/products/{pid}/plot-0.png','label':'Stationary Cyg A and control'}],
                  'metadata_url':f'/api/science/products/{pid}/metadata.json','data_url':f'/api/science/products/{pid}/data.npz',
                  'selection':{'kind':'cyga_transit',**selection},'provenance':{**identity,'samples':len(data['time_unix']),
                  'native_selected_read_bytes':read_bytes,'rendered_at':time.time(),'background':'none',
                  'model_actual_t0':float(exact['times'].unix[0]),'model_actual_t1':float(exact['times'].unix[-1]),
                  'model_time_grid':'Canonical API rounds supplied endpoints down to UTC HH:MM; samples every two minutes.',
                  'beam_api':'casm_vis_analysis.beam_power.beam_power_vs_time','model_api':'bf_weights_generator.plot_transit.array_factor_response'},
                  'warnings':['No static background subtraction. Curves include cross-baseline contaminants and are not a calibration pass/fail.',
                  'Exact model uses ideal equal antenna amplitudes and 32 frequencies across the selected band. Element beam, calibration amplitude taper and channel flag pattern are not modeled.',
                  'Model total normalized power and measured cross-only power have separate axes; their amplitudes are not directly comparable.',
                  'Two-hour cache window may not cover the full crossing; choose background/control windows explicitly before interpretation.',
                  'Layout epoch association inferred from dated CSV; cache has no layout hash. Nominal integration timing is not independently verified.']}
        temp=dest/'metadata.tmp'
        temp.write_text(json.dumps(output,indent=2))
        temp.replace(dest/'metadata.json')
        return output
    finally:
        PLOT_LOCK.release()
