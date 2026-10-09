"""Display-only PPS policy; raw evidence and the saved baseline stay unchanged."""
import math

# Operator-selected absolute offset range against SNAP 0. Keep the audited
# identities/baseline explicit; replacement boards need a fresh decision.
ARRAY_BOARDS = {'192.168.120.52': (0, '0'), '192.168.120.51': (1, '-1'),
                '192.168.120.62': (2, '0'), '192.168.120.73': (3, '-1')}
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

        array_board = (baseline['reference_ip'] == REFERENCE_IP
                       and ARRAY_BOARDS.get(board['ip']) == (board.get('feng_id'), expected))
        accepted = set(range(-2, 3)) if array_board else {int(expected)}
        status.update(accepted_offsets=[str(v) for v in sorted(accepted)],
                      review_offsets=[])
        measured = str(pair[1]) if pair[0] == pair[1] else f'{pair[0]} → {pair[1]}'
        allowed = '−2 to +2' if array_board else expected
        if set(pair) <= accepted:
            status.update(state='ok',label='Within tolerance',
                          detail=f'PPS advancing · {measured} ticks · accepted {allowed} ticks')
        else:
            status.update(state='attention',label='Needs attention',
                          detail=f'PPS offset outside accepted range: {measured} ticks; accepted {allowed} ticks')
