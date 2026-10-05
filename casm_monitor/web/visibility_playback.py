"""Read-only, pinned visibility movies. Numeric chunks, never per-frame PNGs.

Reuse VisStore/casm_io baseline mapping and the existing Sun transform. Only
avg8 shards are read; there is no acquisition, native-file fallback or disk cache.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import warnings
from collections import OrderedDict
from types import SimpleNamespace

import numpy as np
from fastapi import HTTPException, Response

from .. import vis_ops
from ..collectors.vis import DT_S, STREAM_AVG8
from ..store.shards import ensure_contained
from .science_array import beamforming_membership, preview_values
from .vis import VisStore, freq_axis

CHUNK_SIZE = 32
CACHE_BYTES = 256 * 1024**2
MAX_TIMES = 5500  # seven playback days plus one day of preceding context


def sidereal_times(stamps):
    from astropy import units as u
    from astropy.time import Time
    from astropy.utils import iers
    from casm_io.constants import OVRO_LON_DEG
    try:
        with warnings.catch_warnings(), iers.conf.set_temp('auto_download', False), iers.conf.set_temp('auto_max_age', None):
            warnings.simplefilter('ignore', iers.IERSWarning)
            return Time(stamps, format='unix').sidereal_time('mean', longitude=OVRO_LON_DEG*u.deg).to_string(unit=u.hour, sep=':', precision=0, pad=True).tolist()
    except Exception:
        return [None] * len(stamps)


def encode_chunk(meta, values):
    raw = json.dumps(meta, allow_nan=False, separators=(',', ':')).encode()
    raw += b' ' * (-len(raw) % 4)
    return len(raw).to_bytes(4, 'little') + raw + np.asarray(values, dtype='<f4').tobytes()


class PlaybackStore:
    def __init__(self, settings, reader):
        self.settings, self.reader = settings, reader
        self.manifests = OrderedDict()
        self.chunks = OrderedDict()
        self.cache_bytes = 0
        # One bounded cold read at a time; playback never piles up array copies.
        self.lock = threading.RLock()

    def availability(self):
        row = self.reader.query('SELECT MIN(t0) AS start, MAX(t1) AS end FROM shards WHERE stream=?', (STREAM_AVG8,))[0]
        if row['end'] is None:
            raise HTTPException(404, 'No saved visibility history available')
        return dict(start=float(row['start']), end=float(row['end']), integration_s=DT_S)

    def manifest(self, *, inputs, mode='auto', reference_input=8, reference='raw',
                 span_days=1, window_hours=24., end=None, fmin=390.625, fmax=484.375):
        from .science import geometry, select_layout
        if (mode not in ('auto', 'cross', 'matrix') or reference not in ('raw', 'sun')
                or span_days not in (1, 2, 7) or not np.isfinite([window_hours, fmin, fmax]).all()
                or not .1 <= window_hours <= 24 or not 0 < fmin < fmax < 1000
                or not 1 <= len(inputs) <= 32 or len(set(inputs)) != len(inputs)
                or any(not isinstance(i, int) or not 0 <= i < 256 for i in inputs)
                or end is not None and not math.isfinite(end)):
            raise HTTPException(400, 'Choose 1–32 distinct wired inputs, 1/2/7 days, a 0.1–24 h window and increasing MHz bounds')
        available = self.availability()
        end = min(available['end'], end) if end is not None else available['end']
        start, context = end-span_days*86400, end-span_days*86400-window_hours*3600
        layout = select_layout(context, end)
        antennas, _ = geometry(layout['path'])
        antennas.sort(key=lambda a: (-a['position_enu_m'][1], a['position_enu_m'][0]) if a['position_enu_m'] else (math.inf, math.inf))
        wired = {a['packet_idx'] for a in antennas if a['position_enu_m'] is not None}
        if not set(inputs) <= wired or mode == 'cross' and reference_input not in wired:
            raise HTTPException(400, 'Selected inputs and reference must be wired in the dated layout')
        membership = beamforming_membership(self.settings, self.reader, antennas)
        antennas = [dict(a, beamforming=a['packet_idx'] in membership['inputs'] if membership['status']=='complete' else None) for a in antennas]
        selected = [a['packet_idx'] for a in antennas if a['packet_idx'] in inputs]
        pairs = ([(i, j) for k, i in enumerate(selected) for j in selected[k:]] if mode == 'matrix'
                 else [(i, i if mode == 'auto' else reference_input) for i in selected])
        rows = VisStore(self.settings, self.reader).shards.list(STREAM_AVG8, t0=context, t1=end)
        if not rows:
            raise HTTPException(404, 'No saved visibility integrations in this interval')
        axes, times = set(), []
        for row in rows:
            meta, shape = row.get('meta') or {}, row['shape']
            ts = np.asarray(meta.get('t', []), dtype=float)
            ids = meta.get('inputs', [])
            if (len(shape) != 3 or len(ts) != shape[0] or not len(ts) or not np.isfinite(ts).all()
                    or np.any(np.diff(ts) <= 0) or not ids or len(set(ids)) != len(ids)
                    or shape[1] != len(ids)*(len(ids)+1)//2 or not 2 <= shape[2] <= 384
                    or meta.get('nchan') != shape[2] or ts[0] != row['t0'] or ts[-1] != row['t1']):
                raise HTTPException(409, 'Invalid saved visibility axes; no timestamps will be synthesized')
            ensure_contained(row['path'], self.settings.store_root)
            axes.add((shape[2], meta.get('freq_top_mhz'), meta.get('chan_bw_mhz')))
            times.extend(float(t) for t in ts if context <= t <= end)
        if len(axes) != 1:
            raise HTTPException(409, 'Frequency axes changed; choose a shorter interval')
        nchan, top, bw = next(iter(axes))
        if top is None or bw is None or not np.isfinite([top, bw]).all() or bw <= 0:
            raise HTTPException(409, 'Saved frequency axis is unavailable')
        times = np.sort(times)
        if len(times) > MAX_TIMES or len(rows) > MAX_TIMES:
            raise HTTPException(400, 'Saved sequence exceeds the playback read budget')
        if not len(times) or times[-1] < start:
            raise HTTPException(404, 'No saved integrations in the playback period')
        if np.any(np.diff(times) <= 0):
            raise HTTPException(409, 'Duplicate saved integration timestamps')
        freq = freq_axis(rows[0]['meta'], nchan)
        freq = freq[(freq >= fmin) & (freq <= fmax)]
        if len(freq) < 2:
            raise HTTPException(400, 'Select at least two cached frequency channels')
        spec = dict(inputs=selected, mode=mode, reference_input=reference_input, reference=reference,
                    span_days=span_days, window_hours=window_hours, end=end, fmin=fmin, fmax=fmax)
        identity = hashlib.sha256(json.dumps([1, spec, antennas, layout, rows], sort_keys=True, default=str).encode()).hexdigest()[:32]
        with self.lock:
            if identity in self.manifests:
                self.manifests.move_to_end(identity)
                return self.manifests[identity]['public']
            public = dict(id=identity, version=1, selection=spec, inputs=antennas, selected_inputs=selected,
                          pairs=pairs, times=times.tolist(), first_frame=int(np.searchsorted(times, start)),
                          freq_mhz=freq.tolist(), preview_freq_mhz=vis_ops.block_mean(freq, 128).tolist(),
                          start=start, end=float(times[-1]), window_s=window_hours*3600, integration_s=DT_S,
                          chunk_size=CHUNK_SIZE, layout=layout, membership=membership,
                          source_shards=[r['id'] for r in rows], availability=available)
            entry = dict(public=public, rows=rows)
            self.manifests[identity] = entry
            try:
                public['limits'] = self.limits(entry)
                public['lst'] = sidereal_times(times)
                from matplotlib import colormaps
                public['palettes'] = {name: colormaps[cmap](np.linspace(0, 1, 256), bytes=True)[:, :3].tolist()
                                      for name, cmap in [('amp', 'magma'), ('signed', 'RdBu_r'), ('phase', 'twilight_shifted')]}
            except Exception:
                self.manifests.pop(identity, None)
                for key in list(self.chunks):
                    if key[0] == identity:
                        self.cache_bytes -= self.chunks.pop(key).nbytes
                raise
            while len(self.manifests) > 8:
                self.manifests.popitem(last=False)
            return public

    def cube(self, entry, base):
        p = entry['public']; key = (p['id'], base)
        if key in self.chunks:
            self.chunks.move_to_end(key)
            return self.chunks[key]
        stamps = np.asarray(p['times'][base:base+CHUNK_SIZE])
        rows = [r for r in entry['rows'] if r['t1'] >= stamps[0] and r['t0'] <= stamps[-1]]
        store = VisStore(self.settings, self.reader)
        # Immutable manifest rows, not whatever a later collector tick publishes.
        store.shards = SimpleNamespace(list=lambda *a, **kw: rows)
        pairs = [tuple(sorted(pair)) for pair in p['pairs']]
        try:
            z, t, f, _ = store.series(STREAM_AVG8, stamps[0], stamps[-1], pairs, allow_full_fallback=False)
        except Exception as exc:
            raise HTTPException(410, 'Saved visibility shard unavailable; reopen history') from exc
        if len(t) != len(stamps) or not np.array_equal(t, stamps):
            raise HTTPException(409, 'Saved timestamps changed; reopen history')
        spec = p['selection']; mask = (f >= spec['fmin']) & (f <= spec['fmax'])
        if not np.array_equal(f[mask], p['freq_mhz']):
            raise HTTPException(409, 'Saved frequency axis changed; reopen history')
        z = z[:, :, mask]
        if spec['reference'] == 'sun':
            used = sorted({i for pair in pairs for i in pair}); rank = {i:k for k,i in enumerate(used)}
            pos = {a['packet_idx']:a['position_enu_m'] for a in p['inputs']}
            z = vis_ops.fringe_stop_sun(z, f[mask], np.asarray([pos[i] for i in used]),
                                      [(rank[i], rank[j]) for i,j in pairs], t)
        z = np.asarray(z, dtype=np.complex64)
        for k, (i, j) in enumerate(p['pairs']):
            if i > j:
                z[:, k] = z[:, k].conj()
        z[~np.isfinite(z)] = complex(np.nan, np.nan)
        self.chunks[key] = z; self.cache_bytes += z.nbytes
        while self.cache_bytes > CACHE_BYTES:
            self.cache_bytes -= self.chunks.popitem(last=False)[1].nbytes
        return z

    def limits(self, entry):
        p = entry['public']; n = len(p['pairs'])
        low = np.full(n, np.inf); high = np.full(n, -np.inf); positive = np.full(n, np.inf)
        power_low = np.full(n, np.inf); power_high = np.full(n, -np.inf); power_positive = np.full(n, np.inf)
        real = np.zeros(n); imag = np.zeros(n); samples = []
        # Bounded deterministic sample for fixed dynamic colour percentiles.
        stride = max(1, math.ceil(len(p['times'])/128))
        for base in range(0, len(p['times']), CHUNK_SIZE):
            z = self.cube(entry, base); a = np.abs(z)
            low = np.minimum(low, np.min(np.where(np.isfinite(a), a, np.inf), axis=(0, 2)))
            high = np.maximum(high, np.max(np.where(np.isfinite(a), a, -np.inf), axis=(0, 2)))
            positive = np.minimum(positive, np.min(np.where(a > 0, a, np.inf), axis=(0, 2)))
            power_low = np.minimum(power_low, np.min(np.where(np.isfinite(z), z.real, np.inf), axis=(0, 2)))
            power_high = np.maximum(power_high, np.max(np.where(np.isfinite(z), z.real, -np.inf), axis=(0, 2)))
            power_positive = np.minimum(power_positive, np.min(np.where(np.isfinite(z) & (z.real > 0), z.real, np.inf), axis=(0, 2)))
            real = np.maximum(real, np.max(np.where(np.isfinite(z), np.abs(z.real), 0), axis=(0, 2)))
            imag = np.maximum(imag, np.max(np.where(np.isfinite(z), np.abs(z.imag), 0), axis=(0, 2)))
            sample = z[np.arange(len(z))[(np.arange(len(z))+base) % stride == 0]]
            if len(sample):
                v = preview_values(sample)
                samples.append(np.stack([v['real'], v['imag'], v['amp']], axis=-1))
        samples = np.concatenate(samples)
        out = []
        for k in range(n):
            valid = math.isfinite(high[k]); lo = float(low[k]) if valid else 0.; hi = float(high[k]) if valid else 1.
            pad = max((hi-lo)*.05, abs(hi)*.01, 1e-12)
            spectrum = dict(amp=[max(0., lo-pad), hi+pad], amp_log=[float(positive[k])*.95 if math.isfinite(positive[k]) else 1., max(hi*1.05, float(positive[k])*1.01) if math.isfinite(positive[k]) else 10.],
                            real=[-max(float(real[k])*1.05, 1e-12), max(float(real[k])*1.05, 1e-12)],
                            imag=[-max(float(imag[k])*1.05, 1e-12), max(float(imag[k])*1.05, 1e-12)], phase=[-math.pi, math.pi])
            pl, ph = (float(power_low[k]), float(power_high[k])) if math.isfinite(power_high[k]) else (0., 1.)
            pad = max((ph-pl)*.05, abs(ph)*.01, 1e-12)
            spectrum['power'] = [max(0., pl-pad) if pl >= 0 else pl-pad, ph+pad]
            pp = float(power_positive[k])
            spectrum['power_log'] = [pp*.95, max(ph*1.05, pp*1.01)] if math.isfinite(pp) else [1., 10.]
            dynamic = dict(phase=[-math.pi, math.pi])
            for c, name in enumerate(('real', 'imag', 'amp')):
                values = samples[:, k, :, c]; values = values[np.isfinite(values)]
                if name == 'amp':
                    values = values[values > 0]
                    l, h = np.percentile(values, [2, 98]) if len(values) else (1., 10.)
                    dynamic[name] = [float(l), max(float(h), float(l)*1.01)]
                else:
                    h = max(float(np.percentile(np.abs(values), 98)) if len(values) else 1., 1e-12)
                    dynamic[name] = [-h, h]
            values = samples[:, k, :, 0]; values = values[np.isfinite(values)]
            for name, v in (('power', values), ('power_db', 10*np.log10(values[values > 0]))):
                l, h = np.percentile(v, [2, 98]) if len(v) else (0., 1.)
                dynamic[name] = [float(l), max(float(h), float(l)+max(abs(float(l))*.01, 1e-12))]
            out.append(dict(spectrum=spectrum, dynamic=dynamic))
        return out

    def batch(self, identity, base, encoding):
        if encoding not in ('complex', 'preview') or base < 0 or base % CHUNK_SIZE:
            raise HTTPException(400, 'Choose a chunk-aligned frame and complex/preview encoding')
        with self.lock:
            entry = self.manifests.get(identity)
            if entry is None:
                raise HTTPException(410, 'Playback session expired; reopen history')
            self.manifests.move_to_end(identity)
            p = entry['public']
            if base >= len(p['times']):
                raise HTTPException(400, 'Frame is outside the saved sequence')
            z = self.cube(entry, base)
            if encoding == 'preview':
                v = preview_values(z)
                values = np.stack([v['real'], v['imag'], v['amp']], axis=-1)
            else:
                values = np.stack([z.real, z.imag], axis=-1)
            meta = dict(version=1, id=identity, base=base, encoding=encoding, dtype='f32le',
                        shape=list(values.shape), times=p['times'][base:base+len(z)], pairs=p['pairs'])
            return encode_chunk(meta, values)


def register_routes(router, settings, reader):
    playback = PlaybackStore(settings, reader)

    @router.get('/array/history')
    def history():
        return playback.availability()

    @router.get('/array/playback')
    def manifest(inputs: str, mode: str = 'auto', reference_input: int = 8, reference: str = 'raw',
                 span_days: int = 1, window_hours: float = 24, end: float | None = None,
                 fmin: float = 390.625, fmax: float = 484.375):
        try:
            if len(inputs) > 160:
                raise ValueError()
            selected = [int(s) for s in inputs.split(',')]
        except ValueError:
            raise HTTPException(400, 'Use comma-separated packet input IDs')
        return playback.manifest(inputs=selected, mode=mode, reference_input=reference_input, reference=reference,
                                 span_days=span_days, window_hours=window_hours, end=end, fmin=fmin, fmax=fmax)

    @router.get('/array/playback-chunk')
    def chunk(id: str, base: int = 0, encoding: str = 'complex'):
        return Response(playback.batch(id, base, encoding), media_type='application/octet-stream',
                        headers={'Cache-Control': 'private, max-age=86400'})
