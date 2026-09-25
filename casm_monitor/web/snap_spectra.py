"""Full-band, stored SNAP spectra. GETs never acquire data or submit jobs.

Board/ADC identities are stable; station labels describe today's wiring. Arrays
remain the native 12 x 4096 linear-power shards. No Kafka spectra, normalization,
frequency averaging or synthetic gap filling enters these views.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import OrderedDict

import numpy as np
from fastapi import APIRouter, HTTPException, Query

from ..collectors.snapread import snap_read_interval_s
from ..collectors.vis import auto_indices, read_latest_vis
from ..jobs.snap_read import FS_HZ, PPS_PERIOD_TOL, latest_reads
from ..observation import inspected_deployment
from ..store import ShardReader
from ..store.shards import ensure_contained
from .snapread import freq_mhz
from .snaps import board_table

MAX_RECORDS = 20000
READ_BUDGET = 512 * 1024**2


def power_db(values):
    """10 log10(native power); nonpositive/nonfinite channels remain missing."""
    a = np.asarray(values, dtype=float)
    out = np.full(a.shape, np.nan)
    np.log10(a, out=out, where=np.isfinite(a) & (a > 0))
    return out * 10


def json_values(values):
    return [round(float(v), 4) if np.isfinite(v) else None for v in values]


def control_status(summary):
    """Interpret legacy failures conservatively without rewriting stored evidence."""
    errors = summary.get('errors') or {}
    if 'firmware_register_map' in errors or 'connect' in errors:
        return 'unavailable'
    # The old driver conflated a failed management-clock read with unprogrammed.
    if summary.get('programmed') is False and any(k != 'autocorr' for k in errors):
        return 'unavailable'
    if summary.get('programmed') is False:
        return 'firmware_unverified'
    return 'partial' if errors else 'ok' if summary.get('ts') else 'unknown'


def board_health(board, summary, vis, now, interval):
    """Independent data-delivery and timing evidence, never inferred from each other."""
    wired = [i['packet_idx'] for i in board['inputs'] if i['functional'] and i['packet_idx'] is not None]
    present, nonzero = [], []
    if vis:
        autos = np.abs(vis['vis'][auto_indices(len(vis['inputs']))])
        for p in wired:
            if p in vis['inputs']:
                present.append(p)
                a = autos[vis['inputs'].index(p)]
                if np.any(np.isfinite(a) & (a > 0)):
                    nonzero.append(p)
    recent = bool(vis and -60 <= now-vis['ts'] <= 900)
    production = dict(ts=vis['ts'] if vis else None, recent=recent,
                      wired_inputs=len(wired), cached_inputs=len(present), nonzero_inputs=len(nonzero))
    stream_state = ('unknown' if not vis or not wired else 'attention' if not recent
                    else 'ok' if len(nonzero) == len(wired) else 'attention')
    stream_detail = ('No cached visibility evidence' if not vis else
                     f'{len(nonzero)}/{len(wired)} wired inputs have nonzero cached data'
                     + ('' if recent else ' · stale / invalid timestamp'))
    raw, ts = summary.get('pps_raw') or {}, summary.get('ts')
    fresh = ts is not None and -60 <= now-ts <= interval*1.5
    period, count = raw.get('period_pps'), raw.get('count_pps')
    clock = summary.get('fs_hz') or FS_HZ
    readable = (period is not None and count is not None
                and not {'period_pps', 'count_pps'}.intersection(summary.get('errors') or {}))
    period_ok = bool(count and abs(period-clock) <= PPS_PERIOD_TOL*clock) if readable else None
    # A single cached count/rate is not even proof that pulses still advance,
    # and equal nominal rates do NOT establish cross-board sample alignment.
    detail = ('PPS registers unavailable' if period_ok is None else
              'PPS period outside tolerance or no pulses counted' if not period_ok else
              'Last PPS period within tolerance; cross-board alignment not measured')
    if ts and not fresh:
        detail += ' · stale check'
    pps = dict(state='attention' if period_ok is False and fresh else 'unknown',
               alignment='unknown', period_ok=period_ok, period_ticks=period,
               fresh=fresh, ts=ts, detail=detail)
    return dict(ip=board['ip'], feng_id=board['feng_id'], slot=board['slot'],
                control_status=control_status(summary), latest_attempt=ts,
                streaming=dict(state=stream_state, ts=production['ts'], detail=stream_detail),
                pps_status=pps, production_evidence=production)


def aggregate_health(boards, field):
    states = [b[field]['state'] for b in boards]
    state = 'attention' if 'attention' in states else 'ok' if states and all(s == 'ok' for s in states) else 'unknown'
    return dict(state=state, ok=sum(s == 'ok' for s in states), total=len(states))


def timing_evidence(settings):
    """Explicit CLI checks publish only local preview evidence, never GET I/O."""
    if settings.observation_cache_root is None:
        return None
    path = settings.observation_cache_root / 'snap_timing' / 'latest.json'
    try:
        if path.stat().st_size > 65536:
            return None
        report = json.loads(path.read_text())
        if isinstance(report, dict) and report.get('version') == 1 and isinstance(report.get('boards'), dict):
            return report
    except (OSError, ValueError, TypeError):
        pass
    return None


def apply_timing(items, evidence, now):
    if not evidence:
        return
    ts = evidence.get('ts')
    if not isinstance(ts, (int, float)) or not math.isfinite(ts):
        return
    ref = evidence.get('reference_ip')
    if ref not in {b['ip'] for b in items}:
        return
    fresh = -60 <= now-ts <= 5400
    for b in items:
        measured = evidence['boards'].get(b['ip'])
        if not isinstance(measured, dict) or measured.get('state') not in {'ok','attention','unknown'}:
            continue
        # A later board attempt with an actual PPS read failure supersedes an
        # older successful standalone check, rather than retaining a green box.
        superseded = bool(b['latest_attempt'] and b['latest_attempt'] > ts
                          and b['pps_status']['period_ok'] is not True)
        state = measured['state'] if fresh and not superseded else 'unknown'
        period = measured.get('period_ticks')
        period_ok = abs(period-FS_HZ) <= PPS_PERIOD_TOL*FS_HZ if isinstance(period,(int,float)) else None
        b['pps_status'].update(state=state, alignment='aligned' if state=='ok' else 'unknown',
            fresh=fresh, ts=ts, reference_ip=ref, delta_ticks=measured.get('delta_ticks'),
            period_ticks=period, period_ok=period_ok,
            detail=str(measured.get('detail','Alignment unknown')) +
                   (' · stale check' if not fresh else ' · newer PPS read failed' if superseded else ''))


def build_router(settings, reader):
    router = APIRouter(prefix='/api/snap-workspace', tags=['snap-spectra'])
    shards = ShardReader(reader)
    cache = OrderedDict()
    lock = threading.Lock()

    def boards():
        return [b for b in board_table(settings.snap_layout_csv, settings.snap_map_csv)
                if b['role'] == 'antenna']

    @router.get('/beamforming')
    def beamforming():
        """Current recorded CB payload union, mapped to today's board/ADC wiring."""
        deployment = inspected_deployment(settings, reader.latest_scalars())
        known = deployment.get('inspection_state') == 'complete'
        positions = {p['slot']: p['antenna'] for p in deployment.get('positions', [])} if known else {}
        inputs, unresolved = [], []
        for board in boards():
            for inp in board['inputs']:
                slot, antenna = inp['packet_idx'], inp['antenna']
                member = None if not known else False
                if known and slot in positions:
                    member = True if inp['functional'] and antenna == positions[slot] else None
                    if member is None:
                        unresolved.append(slot)
                inputs.append(dict(ip=board['ip'], adc=inp['adc'], antenna=antenna,
                                   packet_idx=slot, beamforming=member))
        mapped = {i['packet_idx'] for i in inputs if i['beamforming'] is True}
        unresolved = sorted(set(unresolved) | (set(positions) - mapped))
        return dict(status='complete' if known and not unresolved else 'unknown',
                    product_id=deployment.get('product_id'), path=deployment.get('path'),
                    live_event_utc=deployment.get('live_event_utc'),
                    inspected_at=deployment.get('inspected_at'),
                    evidence=deployment.get('evidence'), inputs=inputs,
                    beam_count=len(deployment.get('beams', [])), unresolved_slots=unresolved,
                    note='Union of nonzero coherent-beam weights across all beams/channels/polarizations. '
                         'Latest recorded deployment, not a new runtime readback. Current wiring, including in history.')

    @router.get('/health')
    def health():
        """Small live-status response from saved evidence; no hardware contact."""
        now, interval = time.time(), snap_read_interval_s(settings)
        latest, vis = latest_reads(reader), read_latest_vis(settings)
        items = [board_health(b, latest.get(b['ip'], {}), vis, now, interval) for b in boards()]
        apply_timing(items, timing_evidence(settings), now)
        return dict(boards=items, checked_at=now, streaming=aggregate_health(items, 'streaming'),
                    pps=aggregate_health(items, 'pps_status'), streaming_max_age_s=900)

    def records(ip=None, start=None, end=None, *, last=False):
        # Extract only small metadata fields, not twelve EQ coefficient arrays.
        sql = """SELECT id, t0, path, shape, dtype,
            json_extract(meta,'$.ip') ip,
            json_extract(meta,'$.eq_epoch') eq_epoch,
            json_extract(meta,'$.fft_shift') fft_shift
            FROM shards WHERE stream='snap_read'"""
        args = []
        for clause, value in [("json_extract(meta,'$.ip') = ?", ip), ('t0 >= ?', start), ('t0 <= ?', end)]:
            if value is not None:
                sql += ' AND ' + clause
                args.append(value)
        sql += ' ORDER BY t0 ' + ('DESC, id DESC LIMIT 1' if last else 'ASC, id ASC LIMIT ?')
        if not last:
            args.append(MAX_RECORDS + 1)
        rows = [dict(r) for r in reader.query(sql, args)]
        if len(rows) > MAX_RECORDS:
            raise HTTPException(400, 'Too many saved reads; select fewer days')
        return rows

    def preflight(row):
        try:
            shape, dtype = json.loads(row['shape']), np.dtype(row['dtype'])
            if shape != [12, 4096] or dtype.kind not in 'fiu' or dtype.itemsize > 8:
                raise ValueError('expected numeric 12 x 4096 spectrum')
            ensure_contained(row['path'], settings.store_root)
            return math.prod(shape) * dtype.itemsize
        except (ValueError, TypeError) as exc:
            raise HTTPException(409, f'Invalid saved SNAP shard {row["id"]}: {exc}') from exc

    def load(row):
        preflight(row)
        data, _ = shards.load(row['id'])
        if data.shape != (12, 4096):
            raise ValueError('Saved spectrum is not 12 x 4096')
        return np.asarray(data, dtype=float)

    @router.get('/spectra')
    def spectra(at: float | None = None):
        if at is not None and not math.isfinite(at):
            raise HTTPException(400, 'Select a finite acquisition time')
        now, interval = time.time(), snap_read_interval_s(settings)
        latest = latest_reads(reader)
        # Independent, already-stored production evidence. Never a replacement
        # for board spectra and never proof of sample-exact PPS alignment.
        vis = read_latest_vis(settings)
        result = []
        for board in boards():
            ip = board['ip']
            rows = records(ip, end=at, last=True)
            row = rows[0] if rows else None
            # An historical selection is a bounded snapshot, not last-value fill.
            if row and at is not None and at - row['t0'] > interval * 1.5:
                row = None
            item = dict(board, ts=row['t0'] if row else None,
                        shard_id=row['id'] if row else None,
                        eq_epoch=row['eq_epoch'] if row else None,
                        stale=bool(row and now-row['t0'] > interval*1.5),
                        latest_attempt=latest.get(ip, {}).get('ts'),
                        latest_errors=latest.get(ip, {}).get('errors', {}),
                        control_status=control_status(latest.get(ip, {})),
                        error=None, spectra_db=None, zero_channels=None)
            item.update(board_health(board, latest.get(ip, {}), vis, now, interval))
            if row:
                try:
                    data = load(row)
                    item['spectra_db'] = [json_values(line) for line in power_db(data)]
                    item['zero_channels'] = np.sum(data == 0, axis=1).tolist()
                    item['invalid_channels'] = np.sum(~np.isfinite(data) | (data < 0), axis=1).tolist()
                except Exception as exc:
                    item['error'] = f'Saved spectrum unavailable: {exc}'
            result.append(item)
        apply_timing(result, timing_evidence(settings), now)
        return dict(boards=result, freq_mhz=freq_mhz(), channels=4096,
                    units='dB re 1 native power unit (not dBm)',
                    configured_interval_s=interval, selected_at=at)

    @router.get('/spectra-catalog')
    def catalog(days: int = Query(30, ge=1, le=365)):
        end = time.time()
        rows = records(start=end-days*86400, end=end)
        ips = {b['ip'] for b in boards()}
        groups = []
        for row in rows:
            if row['ip'] not in ips:
                continue
            # A sequential board pass can take seven minutes. Keep its real
            # per-board times in spectra(); group only this timeline control.
            if not groups or row['t0'] - groups[-1]['start'] > 600 or row['ip'] in groups[-1]['boards']:
                groups.append(dict(start=row['t0'], at=row['t0'], boards=[]))
            groups[-1]['at'] = row['t0']
            if row['ip'] not in groups[-1]['boards']:
                groups[-1]['boards'].append(row['ip'])
        return dict(days=days, start=end-days*86400, end=end, snapshots=groups,
                    reads=sum(len(g['boards']) for g in groups))

    @router.get('/spectra-trend')
    def trend(ip: str, adc: int = Query(..., ge=0, le=11), days: int = Query(30, ge=1, le=365)):
        if ip not in {b['ip'] for b in boards()}:
            raise HTTPException(404, 'Unknown antenna SNAP')
        end = time.time()
        rows = records(ip, start=end-days*86400, end=end)
        if sum(preflight(row) for row in rows) > READ_BUDGET:
            raise HTTPException(400, 'History exceeds 512 MiB read budget; select fewer days')
        key = (ip, adc, tuple(row['id'] for row in rows))
        # Bounded memory cache, no disk artifacts. Serialize cold history reads.
        with lock:
            if key in cache:
                points = cache.pop(key)
                cache[key] = points
            else:
                points = []
                for row in rows:
                    point = dict(ts=row['t0'], power_db=None, zero_channels=None,
                                 eq_epoch=row['eq_epoch'], error=None)
                    try:
                        values = load(row)[adc]
                        point['zero_channels'] = int(np.sum(values == 0))
                        # Fixed full band: missing channels invalidate the mean,
                        # but genuine zero-power channels contribute zero power.
                        if np.isfinite(values).all() and (values >= 0).all():
                            mean = float(np.mean(values))
                            point['power_db'] = round(10 * math.log10(mean), 4) if mean > 0 else None
                        else:
                            point['error'] = 'Incomplete 4096-channel spectrum'
                    except Exception as exc:
                        point['error'] = str(exc)
                    points.append(point)
                cache[key] = points
                while len(cache) > 8:
                    cache.popitem(last=False)
        return dict(ip=ip, adc=adc, days=days, start=end-days*86400, end=end,
                    points=points, gap_s=snap_read_interval_s(settings)*1.5,
                    units='dB re 1 native power unit', channels=4096)

    return router
