"""Display-only PPS policy; raw evidence and the saved baseline stay unchanged."""
import math

# Audited relayed inputs only. Do not extend this tolerance to new/reassigned
# boards or a different accepted baseline without a new operator decision.
RELAY_BOARDS = {'192.168.120.51': 1, '192.168.120.73': 3}
REFERENCE_IP = '192.168.120.52'
INCONSISTENT = 'Inconsistent cross-board TT delta; alignment unverified'


def offset_pair(raw):
    values = raw.get('delta_ticks_before'), raw.get('delta_ticks')
    try:
        if all(isinstance(v, str) and str(int(v)) == v for v in values):
            return tuple(int(v) for v in values)
    except ValueError:
        pass
    return None


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
        if not raw.get('fresh'):
            status.update(state='unknown')
            continue
        if raw.get('state') not in {'ok', 'attention', 'unknown'}:
            status.update(state='unknown',detail='PPS read state unavailable')
            continue
        pair = offset_pair(raw)
        # This exact reader result is emitted only after the liveness and
        # period gates. Other Unknown results (including later read failures)
        # must never be promoted by the display tolerance.
        inconsistent = (raw.get('state') == 'unknown' and raw.get('detail') == INCONSISTENT
                        and pair is not None and pair[0] != pair[1])
        if raw.get('state') == 'unknown' and not inconsistent:
            status.update(state='unknown')
            continue
        if raw.get('reference_ip') != baseline['reference_ip']:
            status.update(state='attention',detail='PPS reference board changed')
            continue
        if raw.get('state') == 'attention' and not raw.get('detail','').startswith('TT offset '):
            continue  # Stopped PPS, abnormal TT progress or another hard fault.
        if raw.get('period_ok') is False:
            status.update(state='attention',detail='PPS period outside tolerance')
            continue
        if raw.get('period_ok') is not True:
            status.update(state='unknown',detail='PPS period evidence unavailable')
            continue
        if pair is None:
            status.update(state='unknown',detail='Two PPS offset readings unavailable')
            continue

        relay = (baseline['reference_ip'] == REFERENCE_IP and expected == '-1'
                 and board['ip'] in RELAY_BOARDS
                 and board.get('feng_id') == RELAY_BOARDS[board['ip']])
        accepted = {-2, -1} if relay else {int(expected)}
        review = {0} if relay else set()
        status.update(accepted_offsets=[str(v) for v in sorted(accepted)],
                      review_offsets=[str(v) for v in sorted(review)])
        measured = str(pair[1]) if pair[0] == pair[1] else f'{pair[0]} → {pair[1]}'
        allowed = '−2 / −1' if relay else expected
        if set(pair) <= accepted:
            status.update(state='ok',label='Within tolerance',
                          detail=f'PPS advancing · {measured} ticks · accepted {allowed} ticks')
        elif set(pair) <= accepted | review:
            status.update(state='warning',label='Review offset',
                          detail=f'PPS advancing · {measured} ticks · 0-tick offset awaits visibility phase check')
        else:
            status.update(state='attention',label='Needs attention',
                          detail=f'PPS offset outside accepted range: {measured} ticks; accepted {allowed} ticks')
