"""Product-led Sun phase comparisons derived from recorded recipe metadata."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from ..cal_defaults import deployed_product
from ..observation import file_identity, read_json

LOCAL = ZoneInfo('America/Los_Angeles')


def utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc))


def recipe_for(calibration: Path) -> tuple[dict, Path]:
    """Only accept a report whose canonical output identity matches this cal."""
    for report in islice(sorted(calibration.parent.glob('report_*.json')), 64):
        if report.is_symlink():
            continue
        body = read_json(report)
        params = body.get('params') or {}
        if not isinstance(params, dict) or not params.get('out_dir') or not params.get('tag'):
            continue
        expected = Path(params['out_dir']) / f"cal_{params['tag']}.h5"
        if expected.resolve() != calibration.resolve() or params.get('cal_path'):
            continue
        if str(params.get('cal_source', '')).lower() != 'sun':
            continue
        window = params.get('source_window')
        if not isinstance(window, list) or len(window) != 2:
            continue
        try:
            t0, t1 = map(utc, window)
            if not 0 < (t1 - t0).total_seconds() <= 3600:
                continue
        except (TypeError, ValueError):
            continue
        antennas = body.get('antennas') or params.get('antennas')
        if not isinstance(antennas, list) or not antennas or any(type(a) is not int for a in antennas):
            continue
        return dict(params=params, antennas=antennas, t0=t0, t1=t1), report
    raise ValueError('No adjacent Sun report proves the calibration output identity and a solve window of at most one hour. The date is not inferred from the filename.')


def references(settings, comparison_date: str = '', now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    try:
        day = date.fromisoformat(comparison_date) if comparison_date else now.astimezone(LOCAL).date()
    except ValueError as exc:
        raise HTTPException(400, 'comparison_date must be YYYY-MM-DD in OVRO local time') from exc
    product = deployed_product(settings)
    result = dict(references=[], default_id=None, comparison_date=day.isoformat(),
                  warnings=['The ledger calibration is a reference. Mixed factorial beam products can use several solutions.',
                            'Compare Sun-fringe-stopped baseline phase before any new solve or deployment; no saved calibration is applied.',
                            'Matching local clock windows is a starting point, not identical source geometry or unchanged analog conditions.'])
    try:
        if not product.get('cal_file'):
            raise ValueError('No calibration path in the deployment ledger')
        path = Path(product['cal_file'])
        calibration = file_identity(path)
        recipe, report = recipe_for(path)
        t0, t1 = recipe['t0'], recipe['t1']
        local0, local1 = t0.astimezone(LOCAL), t1.astimezone(LOCAL)
        delta_days = (day - local0.date()).days
        compare0 = datetime.combine(local0.date() + timedelta(days=delta_days), local0.timetz(), tzinfo=LOCAL).astimezone(timezone.utc)
        compare1 = datetime.combine(local1.date() + timedelta(days=delta_days), local1.timetz(), tzinfo=LOCAL).astimezone(timezone.utc)
        identity = dict(calibration=calibration, report=file_identity(report),
                        report_sha256=hashlib.sha256(report.read_bytes()).hexdigest(), weights=product.get('weights_file'))
        rid = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
        reason = None
        if compare1 > now:
            reason = 'Today’s matching solar window has not completed. Choose an earlier comparison day.'
        elif (now - compare0).total_seconds() > 7 * 86400:
            reason = 'Choose a comparison day within the past week.'
        elif abs((compare1 - compare0).total_seconds() - (t1 - t0).total_seconds()) > 1:
            reason = 'A daylight-saving transition changes the interval duration; choose another comparison day.'
        reference = dict(id=rid, label=path.name, calibration=calibration,
                         weights=product.get('weights_file'), product_id=product.get('product_id'),
                         deployment_date=product.get('date_deployed'), report=identity['report'],
                         report_sha256=identity['report_sha256'],
                         antennas=recipe['antennas'], recipe_layout=recipe['params'].get('layout_csv'),
                         source='sun', source_window=[t0.isoformat(), t1.isoformat()],
                         source_local_date=local0.date().isoformat(),
                         comparison_window=[compare0.isoformat(), compare1.isoformat()],
                         can_render=reason is None, reason=reason)
        result.update(references=[reference], default_id=rid)
    except (OSError, ValueError, TypeError) as exc:
        result.update(state='unavailable', reason=str(exc))
    else:
        result['state'] = 'ready'
    return result


def build_router(settings) -> APIRouter:
    router = APIRouter()

    @router.get('/api/science/calibration-references')
    def get_references(comparison_date: str = Query('', max_length=10)):
        return references(settings, comparison_date)
    return router
