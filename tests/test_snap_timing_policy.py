"""Board-specific display policy; no hardware, SSH or monitoring DB writes."""
from copy import deepcopy
from itertools import product

import pytest

from casm_monitor.web.snap_spectra import aggregate_health, apply_timing
from casm_monitor.web.snap_timing_status import apply_baseline, INCONSISTENT

IPS = ['192.168.120.52', '192.168.120.51', '192.168.120.62', '192.168.120.73']
TT = 447876382618012342


def evidence(before=-1, after=-1, target=1):
    baseline = dict(version=1, accepted_at=50, reference_ip=IPS[0],
                    offsets=dict(zip(IPS, ['0', '-1', '0', '-1'])))
    report = dict(version=1, ts=100, reference_ip=IPS[0], boards={})
    boards = []
    for n, ip in enumerate(IPS):
        a, b = (before, after) if n == target else ((-1, -1) if n in (1, 3) else (0, 0))
        state, detail = ('unknown', INCONSISTENT) if a != b else (
            ('attention', f'TT offset {b} ticks versus {IPS[0]}') if b else ('ok', 'PPS advancing'))
        report['boards'][ip] = dict(state=state, detail=detail, period_ticks=250000000+b-a,
                                   tt_before=str(TT+a), tt_after=str(TT+250000000+b),
                                   delta_ticks=str(b), count_before=100+n, count_after=101+n)
        boards.append(dict(ip=ip, feng_id=n, latest_attempt=90, pps_status=dict(period_ok=True)))
    apply_timing(boards, report, 101)
    return boards, baseline, report


@pytest.mark.parametrize('target', [1, 3])
@pytest.mark.parametrize('before,after', product(range(-3, 3), repeat=2))
def test_both_frames_determine_relay_colour_without_mutating_evidence(target, before, after):
    boards, baseline, _ = evidence(before, after, target)
    raw = deepcopy([b['pps_status'] for b in boards]); accepted = deepcopy(baseline)
    apply_baseline(boards, baseline)
    pair = {before, after}
    expected = 'ok' if pair <= {-2, -1} else 'warning' if pair <= {-2, -1, 0} else 'attention'
    status = boards[target]['timing_status']
    assert status['state'] == expected
    assert status['label'] == {'ok': 'Within tolerance', 'warning': 'Review offset', 'attention': 'Needs attention'}[expected]
    assert status['delta_ticks_before'] == str(before) and status['delta_ticks'] == str(after)
    assert status['accepted_offsets'] == ['-2', '-1'] and status['review_offsets'] == ['0']
    assert [b['pps_status'] for b in boards] == raw and baseline == accepted


@pytest.mark.parametrize('before,after', product(range(-2, 3), repeat=2))
def test_snap_two_requires_zero_in_both_frames(before, after):
    boards, baseline, _ = evidence(before, after, target=2)
    apply_baseline(boards, baseline)
    assert boards[2]['timing_status']['state'] == ('ok' if before == after == 0 else 'attention')


@pytest.mark.parametrize('change,expected', [
    ({'fresh': False}, 'unknown'),
    ({'state': 'unknown', 'detail': 'Control read unavailable'}, 'unknown'),
    ({'state': 'unknown', 'detail': 'Reference PPS progress unverified'}, 'unknown'),
    ({'state': 'unknown', 'detail': INCONSISTENT+' · newer PPS read failed'}, 'unknown'),
    ({'state': 'attention', 'detail': 'PPS / telescope time did not advance normally'}, 'attention'),
    ({'state': 'attention', 'detail': 'PPS period or TT progress outside tolerance'}, 'attention'),
    ({'period_ok': False}, 'attention'),
    ({'period_ok': None}, 'unknown'),
    ({'reference_ip': IPS[2]}, 'attention'),
    ({'delta_ticks_before': None}, 'unknown'),
    ({'delta_ticks': '0.0'}, 'unknown'),
    ({'delta_ticks': False}, 'unknown'),
    ({'state': 'invalid'}, 'unknown'),
])
def test_tolerance_never_overrides_freshness_liveness_reference_or_parse_failures(change, expected):
    boards, baseline, _ = evidence(-1, 0)
    boards[1]['pps_status'].update(change)
    apply_baseline(boards, baseline)
    assert boards[1]['timing_status']['state'] == expected


def test_equal_pair_with_inconsistent_reader_state_stays_unknown():
    boards, baseline, _ = evidence(-1, -1)
    boards[1]['pps_status'].update(state='unknown', detail=INCONSISTENT)
    apply_baseline(boards, baseline)
    assert boards[1]['timing_status']['state'] == 'unknown'


def test_changed_board_identity_does_not_inherit_tolerance():
    boards, baseline, _ = evidence(-2, -2)
    boards[1]['feng_id'] = 9
    apply_baseline(boards, baseline)
    assert boards[1]['timing_status']['state'] == 'attention'
    boards, baseline, _ = evidence(-2, -2)
    baseline['offsets'][IPS[1]] = '0'
    apply_baseline(boards, baseline)
    assert boards[1]['timing_status']['state'] == 'attention'


def test_real_large_integer_pair_expiry_and_new_failure():
    boards, baseline, report = evidence(-2, -1)
    apply_baseline(boards, baseline)
    assert boards[1]['timing_status']['state'] == 'ok'
    assert boards[1]['timing_status']['delta_ticks_before'] == '-2'
    assert boards[1]['pps_status']['state'] == 'unknown'
    apply_timing(boards, report, 5501)
    apply_baseline(boards, baseline)
    assert boards[1]['timing_status']['state'] == 'unknown'
    boards[1].update(latest_attempt=102, pps_status=dict(period_ok=None))
    apply_timing(boards, report, 103)
    apply_baseline(boards, baseline)
    assert boards[1]['timing_status']['state'] == 'unknown'


@pytest.mark.parametrize('states,expected', [
    (['ok']*4, 'ok'), (['ok', 'warning', 'ok', 'warning'], 'warning'),
    (['ok', 'warning', 'unknown', 'ok'], 'unknown'),
    (['attention', 'warning', 'unknown', 'ok'], 'attention'), ([], 'unknown'),
])
def test_summary_does_not_count_yellow_as_green_or_hide_failed_reads(states, expected):
    result = aggregate_health([dict(timing_status=dict(state=s)) for s in states], 'timing_status')
    assert result == dict(state=expected, ok=states.count('ok'), total=len(states))
