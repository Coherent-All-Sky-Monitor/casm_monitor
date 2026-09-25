"""Dated, reviewed source evidence beside the independent exact-PPS check.

No source detection is inferred here. A reviewed local record can qualify an
unchanged PPS offset only while timing/source evidence and identities are fresh.
No hardware, native visibility reads, solves or automatic repairs occur.
"""
import json
import math

from ..observation import file_identity, inspected_deployment

MAX_AGE_S = 5400


def read_check(settings):
    if settings.observation_cache_root is None:
        return None
    path = settings.observation_cache_root / 'snap_source_check.json'
    try:
        if path.stat().st_size > 65536:
            return None
        result = json.loads(path.read_text())
        if (isinstance(result, dict) and result.get('version') == 1
                and result.get('reviewed') is True and isinstance(result.get('boards'), dict)):
            return result
    except (OSError, ValueError, TypeError):
        pass
    return None


def apply_source_check(items, evidence, deployment, now):
    """Keep pps_status unchanged; expose a separately labelled combined assessment."""
    for board in items:
        board['timing_source_status'] = dict(board['pps_status'])
    if not evidence:
        return
    try:
        end = evidence['t1']
        valid = (isinstance(end, (int, float)) and math.isfinite(end)
                 and -60 <= now-end <= MAX_AGE_S
                 and deployment.get('inspection_state') == 'complete'
                 and deployment.get('product_id') == evidence['product_id']
                 and deployment.get('path') == evidence['weights']['path']
                 and all(file_identity(evidence[k]['path']) == evidence[k]
                         for k in ('weights', 'calibration', 'layout')))
    except (KeyError, OSError, TypeError, ValueError):
        return
    if not valid:
        return
    ref = evidence.get('reference_ip')
    for board in items:
        pps = board['pps_status']
        check = evidence.get('boards', {}).get(board['ip'])
        if not isinstance(check, dict) or not isinstance(check.get('source'), str):
            continue
        if (pps.get('state') != 'attention' or pps.get('fresh') is not True
                or pps.get('period_ok') is not True or pps.get('reference_ip') != ref
                or pps.get('delta_ticks') != check.get('delta_ticks')
                or not pps.get('detail', '').startswith('TT offset ')):
            continue
        board['timing_source_status'] = dict(
            state='ok', label='Source coherence seen', ts=end,
            detail=f"{pps['delta_ticks']} PPS tick · {check['source']} ↔ SNAP 0/2",
            source=check['source'], delta_ticks=pps['delta_ticks'],
            timing_ts=pps['ts'], exact_alignment=False,
            note='Dated board-group source check, not exact sample alignment or an all-input health pass.')


def annotate(settings, reader, items, now):
    evidence = read_check(settings)
    if evidence:
        try:
            if file_identity(settings.snap_layout_csv) != evidence.get('layout'):
                evidence = None
        except (OSError, TypeError):
            evidence = None
    deployment = inspected_deployment(settings, reader.latest_scalars()) if evidence else {}
    apply_source_check(items, evidence, deployment, now)
