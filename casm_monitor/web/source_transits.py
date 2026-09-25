"""Read-only fixed transit beams from bounded, native cached visibilities.

Apply the current ledger calibration at native channels through the maintained
beam API. No solve, voltage/filterbank read, acquisition or persistent write.
"""
from __future__ import annotations

import json
import hashlib
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np
from fastapi import HTTPException

from ..collectors.vis import DT_S, STREAM_FULL
from ..observation import file_identity
from ..vis_ops import block_mean
from .science import select_layout
from .science_array import _LOCK, finite_list, image_tile
from .science_transit import calibration_catalog, native_inputs
from .vis import VisStore, _shard_meta_times

SOURCES = {'sun': 'Sun', 'cyg_a': 'Cyg A', 'cas_a': 'Cas A', 'tau_a': 'Tau A'}
HALF_WINDOW = 7200
MAX_DAYS = 7
MAX_TRANSITS = 3
FIGURE_VERSION = 'stationary-cross-power-v2'
_CACHE: OrderedDict[tuple, dict] = OrderedDict()


def source_key(query: str) -> str | None:
    name = query.strip().lower().replace('-', '').replace('_', '').replace(' ', '')
    return {'sun': 'sun', 'cyga': 'cyg_a', 'cygnusa': 'cyg_a',
            'casa': 'cas_a', 'cassiopeiaa': 'cas_a',
            'taua': 'tau_a', 'taurusa': 'tau_a', 'crab': 'tau_a'}.get(name)


@lru_cache(maxsize=128)
def transit_time(source: str, day: str) -> float:
    from casm_vis_analysis.calibration_checks import transit_center
    if source not in SOURCES:
        raise HTTPException(404, 'Unknown visibility source')
    try:
        parsed = datetime.strptime(day, '%Y-%m-%d')
        if parsed.strftime('%Y-%m-%d') != day:
            raise ValueError()
    except ValueError:
        raise HTTPException(400, 'Use a YYYY-MM-DD UTC transit date')
    return transit_center(source, day)


def catalog(settings, reader, source: str) -> dict:
    if source not in SOURCES:
        raise HTTPException(404, 'Unknown visibility source')
    cals = calibration_catalog(settings)
    cal = {**cals[0], 'name': Path(cals[0]['path']).name} if cals else None
    span = reader.query('SELECT MIN(t0) AS t0, MAX(t1) AS t1 FROM shards WHERE stream=?', (STREAM_FULL,))
    rows = []
    coverage = None
    if span and span[0]['t0'] is not None:
        first, last = float(span[0]['t0']), float(span[0]['t1'])
        coverage = dict(t0=first, t1=last, stream=STREAM_FULL)
        day = datetime.fromtimestamp(last, timezone.utc).date()
        store = VisStore(settings, reader)
        for offset in range(MAX_DAYS):
            date = (day - timedelta(days=offset)).isoformat()
            peak = transit_time(source, date)
            if peak + HALF_WINDOW < first:
                break
            # Publish finished four-hour windows, not an ever-changing live beam.
            if peak + HALF_WINDOW > last:
                continue
            shards = store.shards.list(STREAM_FULL, t0=peak-HALF_WINDOW, t1=peak+HALF_WINDOW)
            times = sorted({t for r in shards for t in _shard_meta_times(r)
                            if peak-HALF_WINDOW <= t <= peak+HALF_WINDOW})
            if len(times) >= 3:
                layout = select_layout(peak-HALF_WINDOW, peak+HALF_WINDOW)
                signature = json.dumps([FIGURE_VERSION, source, date, cal,
                                        file_identity(layout['path'])], sort_keys=True)
                rows.append(dict(date=date, transit_unix=peak, t0=times[0], t1=times[-1],
                                 cache_key=hashlib.sha256(signature.encode()).hexdigest(),
                                 samples=len(times), partial=times[0] > peak-HALF_WINDOW+DT_S
                                 or times[-1] < peak+HALF_WINDOW-DT_S))
                if len(rows) == MAX_TRANSITS:
                    break
    return dict(kind='visibility_beam', state='ready' if rows and cal else 'unavailable',
                source=source, name=SOURCES[source], calibration=cal, transits=rows,
                coverage=coverage, window_hours=2*HALF_WINDOW/3600, max_days=MAX_DAYS,
                beam_mode='stationary_transit', max_transits=MAX_TRANSITS,
                completed_windows_only=True)


def beam_spectrum(data, mapping, cal, source, *, pointing=None):
    """Native signed cross-power; flagged channels keep their frequency positions."""
    from casm_vis_analysis.beam_power import beam_power_vs_time
    freq = np.asarray(data['freq_mhz'])
    cf = np.asarray(cal.frequencies_hz)/1e6
    good = np.asarray(cal.flags, dtype=bool)
    weights = np.asarray(cal.weights)
    if (freq.ndim != 1 or len(freq) < 2 or cf.shape != freq.shape
            or good.shape != freq.shape or weights.shape != (len(cal.ant_ids), len(freq))
            or not np.isfinite(cf).all()):
        raise HTTPException(409, 'Calibration requires matching native frequency, flag and antenna-weight arrays')
    if cf[0] < cf[-1]:
        cf, good, weights = cf[::-1], good[::-1], weights[:, ::-1]
    if cf.shape != freq.shape or not np.allclose(cf, freq, atol=1e-5, rtol=0):
        raise HTTPException(409, 'Calibration channels do not match the native visibility cache')
    good = good & np.isfinite(weights).all(axis=0)
    if np.count_nonzero(good) < 2:
        raise HTTPException(409, 'Calibration has fewer than two usable channels')
    # A supplied alt/az remains fixed throughout the observation. Do not abs():
    # negative values are real measurements, not invalid intensity samples.
    direction = source if pointing is None else (source, *pointing)
    result = beam_power_vs_time({**data, 'freq_mask': ~good}, mapping,
                                sources=[direction], cal_weights=cal, sign=-1, return_spectrum=True)
    full = np.full((len(data['time_unix']), len(freq)), np.nan)
    full[:, good] = result['spectrum'][source]
    return full, good


def window_spectrum(settings, reader, mapping, ants, cal, source, pointing, rows, t0, t1):
    """Stream a four-hour window in bounded native reads, retaining only power."""
    stamps = sorted({t for row in rows for t in _shard_meta_times(row) if t0 <= t <= t1})
    if not 3 <= len(stamps) <= 110:
        raise HTTPException(400, 'Transit requires 3–110 native integrations in the four-hour window')
    parts, times, used = [], [], {}
    total_bytes = 0
    freq = good = None
    # Balanced groups avoid one/two-sample tails and cap each read at 26 samples.
    for group in np.array_split(stamps, int(np.ceil(len(stamps)/26))):
        data, compact, shards, size = native_inputs(reader, mapping, ants, float(group[0]),
                                                  float(group[-1]), settings, preserve_missing=True)
        if not np.isfinite(compact.dataframe[['x_m','y_m','z_m']].to_numpy()).all():
            raise HTTPException(409, 'Dated layout has missing antenna positions')
        power, mask = beam_spectrum(data, compact, cal, source, pointing=pointing)
        if freq is not None and (not np.array_equal(freq, data['freq_mhz']) or not np.array_equal(good, mask)):
            raise HTTPException(409, 'Native channel axis or calibration mask changed across the window')
        freq, good = np.asarray(data['freq_mhz']), mask
        parts.append(power)
        times.append(np.asarray(data['time_unix']))
        total_bytes += size
        used.update((r['id'],r) for r in shards)
        del data, compact
    times = np.concatenate(times)
    if np.any(np.diff(times) <= 0):
        raise HTTPException(409, 'Overlapping native integrations across transit chunks')
    return np.concatenate(parts), good, times, freq, list(used.values()), total_bytes


def snapshot(settings, reader, source: str, day: str, calibration_id: str) -> dict:
    from bf_weights_generator import load_calibration_weights
    from casm_io.correlator.mapping import AntennaMapping
    from casm_vis_analysis.sources import source_altaz
    peak = transit_time(source, day)
    # Only allow the current catalogue, never user-supplied paths or a quiet
    # fallback to a different calibration after a deployment changes.
    item = next((c for c in calibration_catalog(settings) if c['id'] == calibration_id), None)
    if item is None:
        raise HTTPException(409, 'Current calibration changed or is unavailable; refresh source history')
    if item['size'] > 32 * 1024**2:
        raise HTTPException(400, 'Calibration exceeds the 32 MiB metadata limit')
    t0, t1 = peak-HALF_WINDOW, peak+HALF_WINDOW
    layout = select_layout(t0, t1)
    identity = file_identity(layout['path'])
    rows = VisStore(settings, reader).shards.list(STREAM_FULL, t0=t0, t1=t1)
    if not rows:
        raise HTTPException(404, 'Native visibility cache unavailable for this transit')
    key = (str(settings.store_root), source, day, calibration_id, json.dumps(identity, sort_keys=True),
           tuple((r['id'], r['t1']) for r in rows))
    # Share the array-overview read lock: no simultaneous large display cubes.
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
        cal = load_calibration_weights(item['path'])
        mapping = AntennaMapping.load(layout['path'])
        ants = list(map(int, cal.ant_ids))
        wired = set(mapping.dataframe.loc[mapping.dataframe.functional == 1, 'antenna_id'].astype(int))
        if not 2 <= len(ants) <= 32 or len(set(ants)) != len(ants) or not set(ants) <= wired:
            raise HTTPException(409, 'All calibration antennas must be distinct and wired in the dated layout; no silent subset')
        altitude, azimuth = source_altaz(source, np.array([peak]))
        pointing = (float(altitude[0]), float(azimuth[0]))
        power, good, times, freq, used, read_bytes = window_spectrum(
            settings, reader, mapping, ants, cal, source, pointing, rows, t0, t1)
        if not np.isfinite(power).any():
            raise HTTPException(404, 'No complete calibrated cross-baseline samples for this transit')
        preview = block_mean(power, 128)
        # Same native-channel mean as the maintained beam API, not a mean of
        # display bins (which can contain different numbers of good channels).
        # A missing good-channel measurement leaves a gap, not a changed band.
        broadband = np.mean(power[:, good], axis=1)
        if file_identity(item['path']) != {k:item[k] for k in ('path','size','mtime_ns')} or file_identity(layout['path']) != identity:
            raise HTTPException(409, 'Calibration or layout changed during the read; refresh source history')
        result = dict(source=source, name=SOURCES[source], date=day, transit_unix=peak,
                      t0=float(times[0]), t1=float(times[-1]), freq_mhz=freq.tolist(), integration_s=DT_S,
                      tile=image_tile(preview, times, 'real'), samples=len(times), antenna_ids=ants,
                      beam_mode='stationary_transit', window_hours=2*HALF_WINDOW/3600,
                      pointing=dict(alt_deg=pointing[0], az_deg=pointing[1], time_unix=peak),
                      partial=bool(times[0] > t0+DT_S or times[-1] < t1-DT_S),
                      calibration={**item, 'name':Path(item['path']).name},
                      preview=dict(time_unix=times.tolist(), freq_mhz=block_mean(freq,128).tolist(),
                                   cross_power=finite_list(preview)),
                      light_curve=dict(time_unix=times.tolist(), cross_power=finite_list(broadband),
                                       channels=int(good.sum()),
                                       freq_range_mhz=[float(freq[good].min()), float(freq[good].max())]),
                      details=dict(beam='Stationary at the source transit alt/az; signed cross-only power, autos excluded',
                                   units='Calibration-weighted correlator counts, not Jy',
                                   calibration_policy='Current ledger calibration applied to every displayed date, not the calibration deployed on that date',
                                   background='No static or off-source subtraction',
                                   averaging='Calibration and beamforming at native channels, then frequency-only display averaging to 128 bins',
                                   light_curve='Equal-weight mean over the fixed set of usable native channels; no time smoothing; missing good channels leave null samples',
                                   pointing='Fixed geometric phasing plus the current calibration; not a replay of uploaded quantized hardware weights',
                                   window='±2 hours around transit; native reads in groups of at most 26 integrations; no raw archive fallback',
                                   gaps='Missing baselines propagate to missing beam samples; no antenna substitution or file-boundary exclusion',
                                   good_channels=int(good.sum()), native_channels=len(freq), layout={**layout, **identity},
                                   stream=STREAM_FULL, shard_ids=[r['id'] for r in used], selected_read_bytes=read_bytes,
                                   api='casm_vis_analysis.beam_power.beam_power_vs_time'))
        _CACHE[key] = result
        while len(_CACHE) > 12:
            _CACHE.popitem(last=False)
        return result
