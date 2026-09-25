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
from ..jobs.snap_read import latest_reads
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


def build_router(settings, reader):
    router = APIRouter(prefix='/api/snap-workspace', tags=['snap-spectra'])
    shards = ShardReader(reader)
    cache = OrderedDict()
    lock = threading.Lock()

    def boards():
        return [b for b in board_table(settings.snap_layout_csv, settings.snap_map_csv)
                if b['role'] == 'antenna']

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
                        error=None, spectra_db=None, zero_channels=None)
            if row:
                try:
                    data = load(row)
                    item['spectra_db'] = [json_values(line) for line in power_db(data)]
                    item['zero_channels'] = np.sum(data == 0, axis=1).tolist()
                    item['invalid_channels'] = np.sum(~np.isfinite(data) | (data < 0), axis=1).tolist()
                except Exception as exc:
                    item['error'] = f'Saved spectrum unavailable: {exc}'
            result.append(item)
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
