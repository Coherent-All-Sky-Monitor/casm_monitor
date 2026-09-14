"""Selected SNAP history rendered with the shared solar plotting implementation.

No acquisition: this adapter invokes the existing history reader and bounds its
whole-shard reads before calling it. Full-band board overviews remain existing
collector figures, with their real acquisition times.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..observation import cache_dir
from ..store import ShardReader
from .snaps import build_router as snap_router, _time_arg, db_to_linear, STREAM_FULL, STREAM_SUB, SUB_SPAN_S

_LOCK = threading.Lock()


class Selection(BaseModel):
    packet_idx: int
    t0: str
    t1: str
    fmin: float = 390.0
    fmax: float = 485.0


def build_router(settings, reader):
    router = APIRouter()
    # Keep the existing history contract as the single data-loading implementation.
    history = next(r.endpoint for r in snap_router(settings, reader).routes if r.path == '/api/snaps/history')

    @router.get('/api/snap-workspace/board-overview')
    def board_overview():
        """Restyle the latest stored board spectra; never contact hardware."""
        if settings.observation_cache_root is None:
            raise HTTPException(403, 'Explicit isolated artifact root required')
        from ..figures.snap_figures import board_table, render_kind
        from ..jobs.snap_read import latest_reads
        from .science import PLOT_LOCK

        boards = board_table()
        reads = latest_reads(reader)
        if not reads:
            raise HTTPException(404, 'No saved board spectra yet')
        snapshots = [dict(ip=b['ip'], feng_id=b.get('feng_id'),
                          **{k: reads.get(b['ip'], {}).get(k) for k in ('ts', 'shard_id', 'errors')})
                     for b in boards if b.get('role') == 'antenna']
        code = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in
                (Path(__file__), Path(__file__).parent.parent / 'figures' / 'snap_figures.py')}
        identity = hashlib.sha256(json.dumps([1, code, boards, snapshots], sort_keys=True).encode()).hexdigest()[:24]
        root = cache_dir(settings) / 'snap_views'
        out = root / identity
        if root.is_symlink() or out.is_symlink():
            raise HTTPException(403, 'Symlink artifact directories forbidden')
        with _LOCK:
            if not (out / 'metadata.json').exists():
                with PLOT_LOCK:
                    pngs, info = render_kind(reader, settings, 'spectra_board', 'all12', boards=boards, dark=True)
                out.mkdir(parents=True, exist_ok=True)
                (out / 'spectrum.png').write_bytes(pngs['2x'])
                body = dict(id=identity, selection={'source': 'Saved 4096-channel board spectra'},
                            images=[dict(url=f'/api/snap-workspace/{identity}/spectrum.png', label='SNAP spectra · power (dB) vs frequency (MHz)')],
                            metadata_url=f'/api/snap-workspace/{identity}/metadata.json',
                            provenance=dict(renderer='casm_monitor.figures.snap_figures.render_spectra_board', code=code, boards=snapshots, **info),
                            warnings=['Each input has its own power scale. Shading marks the transmitted band.',
                                      'Acquisition timestamps are per board; a new render does not mean a new acquisition.'])
                temp = out / 'metadata.tmp'
                temp.write_text(json.dumps(body, indent=2))
                temp.replace(out / 'metadata.json')
            return json.loads((out / 'metadata.json').read_text())

    @router.post('/api/snap-workspace/render')
    def render(req: Selection):
        if settings.observation_cache_root is None:
            raise HTTPException(403, 'Explicit isolated artifact root required')
        start, end = _time_arg('t0', req.t0), _time_arg('t1', req.t1)
        if start is None or end is None or not np.isfinite([start,end,req.fmin,req.fmax]).all() or not 0 < end-start <= 7*86400:
            raise HTTPException(400, 'Select a finite interval of at most seven days')
        if not 0 < req.fmin < req.fmax < 1000:
            raise HTTPException(400, 'Select ordered finite frequency bounds in MHz')
        stream = STREAM_SUB if end-start > SUB_SPAN_S else STREAM_FULL
        shards = ShardReader(reader).list(stream, t0=start, t1=end, limit=10001)
        estimated = 0
        try:
            for shard in shards:
                shape = shard['shape']
                if not isinstance(shape, list) or len(shape) != 3 or any(type(dim) is not int or dim <= 0 for dim in shape):
                    raise ValueError('Expected three positive integer shape dimensions')
                dtype = np.dtype(shard['dtype'])
                if dtype.kind not in 'iuf' or dtype.itemsize <= 0:
                    raise ValueError('Expected a fixed-size numeric shard dtype')
                estimated += math.prod(shape) * dtype.itemsize
        except (TypeError, ValueError, KeyError) as exc:
            raise HTTPException(400, f'SNAP shard manifest cannot establish a safe read budget: {exc}') from exc
        if estimated > 256*1024**2 or len(shards)>10000:
            raise HTTPException(400, 'SNAP history exceeds 256 MiB read budget; narrow the time interval')
        selection = req.model_dump()
        identity = hashlib.sha256(json.dumps([3,selection,[(s['id'],s['t1']) for s in shards]],sort_keys=True).encode()).hexdigest()[:24]
        display_bin_s = 60 if stream == STREAM_FULL else 10
        root = cache_dir(settings) / 'snap_views'
        out = root / identity
        if out.is_symlink() or root.is_symlink():
            raise HTTPException(403, 'Symlink artifact directories forbidden')
        with _LOCK:
            if not (out/'metadata.json').exists():
                data = history(packet_idx=req.packet_idx,t0=req.t0,t1=req.t1,source='kafka',max_cells=700000)
                if not data['t']:
                    raise HTTPException(404, 'No cached transmitted-band history in this interval')
                if data.get('n_samples_raw',len(data['t'])) != len(data['t']):
                    raise HTTPException(400, 'Time averaging would hide gaps; select a shorter SNAP interval')
                times, freq, z = np.asarray(data['t']), np.asarray(data['freq_mhz']), np.asarray(data['z_db'])
                if np.any(np.diff(times)<=0):
                    raise HTTPException(409, 'Duplicate or unordered SNAP timestamps; choose a narrower interval')
                keep = (freq>=req.fmin)&(freq<=req.fmax)
                if keep.sum()<2:
                    raise HTTPException(400, 'Frequency selection contains fewer than two cached channels')
                from casm_vis_analysis.solar_waterfall import plot_dynamic_spectrum
                from .science import PLOT_LOCK
                out.mkdir(parents=True,exist_ok=True)
                with PLOT_LOCK:
                    import matplotlib.pyplot as plt
                    try:
                        with plt.style.context('dark_background'):
                            fig=plot_dynamic_spectrum(db_to_linear(z[:,keep]),times,freq[keep],
                                                      title=f'SNAP transmitted-band history · packet input {req.packet_idx}',
                                                      quantity='Power',chans=None,tz='America/Los_Angeles',
                                                      integration_s=display_bin_s)
                            fig.savefig(out/'spectrum.png',dpi=120,bbox_inches='tight',facecolor='#000000')
                            plt.close(fig)
                    except ValueError as exc:
                        raise HTTPException(400,f'Selected SNAP data cannot be rendered: {exc}') from exc
                np.savez_compressed(out/'data.npz',time_unix=times,freq_mhz=freq[keep],power_db=z[:,keep])
                body = dict(id=identity,selection=selection,images=[dict(url=f'/api/snap-workspace/{identity}/spectrum.png',label='Transmitted-band history · channel-normalized waterfall, raw bandpass and light curves')],
                            metadata_url=f'/api/snap-workspace/{identity}/metadata.json',data_url=f'/api/snap-workspace/{identity}/data.npz',
                            provenance=dict(source_stream=data['stream'],resolution=data['res'],observed_start=float(times.min()),observed_end=float(times.max()),
                                            estimated_read_bytes=estimated,shard_ids=[s['id'] for s in shards],reader='casm_monitor.web.snaps.history',
                                            display_sampling_width_s=display_bin_s,physical_integration_s=None,
                                            renderer='casm_vis_analysis.solar_waterfall.plot_dynamic_spectrum'),
                            warnings=['Transmitted-band cache, not a new 4096-channel board acquisition. Channel means normalize the displayed interval; compare raw spectra within an EQ epoch.',
                                      'Plot bin width follows the nominal stored sampling cadence, not an independently measured physical integration duration.'])
                temp=out/'metadata.tmp';temp.write_text(json.dumps(body,indent=2));temp.replace(out/'metadata.json')
            return json.loads((out/'metadata.json').read_text())

    @router.get('/api/snap-workspace/{identity}/{filename}')
    def artifact(identity: str,filename: str):
        import re
        if not re.fullmatch('[0-9a-f]{24}',identity) or filename not in {'spectrum.png','metadata.json','data.npz'}:
            raise HTTPException(404,'Unknown artifact')
        root=cache_dir(settings)/'snap_views'
        path=root/identity/filename
        if path.is_symlink() or root.resolve() not in path.resolve().parents or not path.is_file():
            raise HTTPException(404,'Artifact unavailable')
        return FileResponse(path,filename=filename,content_disposition_type='inline' if filename.endswith('.png') else 'attachment')
    return router
