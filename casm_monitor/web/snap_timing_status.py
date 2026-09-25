"""PPS stability against explicitly accepted offsets, not source detection."""
import math


def valid_baseline(baseline, boards):
    if not isinstance(baseline, dict) or baseline.get('version') != 1:
        return False
    offsets = baseline.get('offsets')
    accepted = baseline.get('accepted_at')
    if (not isinstance(offsets, dict) or set(offsets) != {b['ip'] for b in boards}
            or baseline.get('reference_ip') not in offsets
            or not isinstance(accepted, (int, float)) or not math.isfinite(accepted)):
        return False
    try:
        return (all(isinstance(v, str) and str(int(v)) == v for v in offsets.values())
                and offsets[baseline['reference_ip']] == '0')
    except ValueError:
        return False


def apply_baseline(boards, baseline):
    valid = valid_baseline(baseline, boards)
    for board in boards:
        raw = board['pps_status']
        status = dict(raw)
        board['timing_status'] = status
        if not valid:
            status.update(state='unknown',detail='PPS timing reference unavailable')
            continue
        expected = baseline['offsets'][board['ip']]
        status.update(baseline_delta_ticks=expected, baseline_accepted_at=baseline['accepted_at'])
        if not raw.get('fresh') or raw.get('state') == 'unknown':
            status.update(state='unknown')
            continue
        if raw.get('reference_ip') != baseline['reference_ip']:
            status.update(state='attention',detail='PPS reference board changed')
        elif raw.get('delta_ticks') != expected:
            status.update(state='attention',detail=f"PPS offset changed: {expected} → {raw.get('delta_ticks','unknown')} ticks")
        elif raw.get('period_ok') is True and (raw.get('state') == 'ok'
                or raw.get('state') == 'attention' and raw.get('detail','').startswith('TT offset ')):
            status.update(state='ok', label='OK',
                          detail=f'PPS advancing · {expected} ticks · unchanged from accepted reference')
        elif raw.get('state') == 'ok':
            status.update(state='unknown',detail='PPS period evidence unavailable')
