from types import SimpleNamespace

import pytest

from casm_monitor.observation import file_identity
from casm_monitor.web.snap_source_check import apply_source_check, read_check


@pytest.fixture
def evidence(tmp_path):
    identities = {}
    for name in ('weights', 'calibration', 'layout'):
        path = tmp_path / name
        path.write_text(name)
        identities[name] = file_identity(path)
    return dict(version=1, reviewed=True, t0=50, t1=100, product_id='p', reference_ip='ref',
                boards={'board': dict(source='Cas A', delta_ticks='-1')}, **identities)


def board(**changes):
    return dict(ip='board', pps_status=dict(state='attention', fresh=True, period_ok=True,
                reference_ip='ref', delta_ticks='-1', detail='TT offset -1 ticks versus ref', ts=100, **changes))


def apply(evidence, item=None, now=101, **deployment_changes):
    item = item or board()
    deployment = dict(inspection_state='complete', product_id='p', path=evidence['weights']['path'])
    deployment.update(deployment_changes)
    apply_source_check([item], evidence, deployment, now)
    return item


def test_reviewed_source_check_is_separate_from_exact_pps(evidence):
    result = apply(evidence)
    assert result['timing_source_status']['state'] == 'ok'
    assert result['timing_source_status']['label'] == 'Source coherence seen'
    assert result['pps_status']['state'] == 'attention'
    assert result['timing_source_status']['exact_alignment'] is False
    assert result['timing_source_status']['delta_ticks'] == '-1'


@pytest.mark.parametrize('change', [dict(state='unknown'), dict(fresh=False), dict(period_ok=False),
    dict(delta_ticks='-2'), dict(reference_ip='different'), dict(detail='PPS / telescope time did not advance normally')])
def test_source_image_never_overrides_changed_or_failed_timing(evidence, change):
    item = board()
    item['pps_status'].update(change)
    out = apply(evidence, item)
    assert out['timing_source_status'] == out['pps_status']


def test_stale_source_changed_cal_or_deployment_invalidates_green(evidence):
    assert apply(evidence, now=5501)['timing_source_status']['state'] == 'attention'
    assert apply(evidence, product_id='changed')['timing_source_status']['state'] == 'attention'
    assert apply(evidence, inspection_state='pending')['timing_source_status']['state'] == 'attention'
    evidence['calibration']['mtime_ns'] -= 1
    assert apply(evidence)['timing_source_status']['state'] == 'attention'


def test_missing_corrupt_or_unreviewed_record(tmp_path):
    settings = SimpleNamespace(observation_cache_root=tmp_path)
    assert read_check(settings) is None
    p = tmp_path/'snap_source_check.json'
    for text in ('[]', 'bad json', '{"version":1,"reviewed":false}'):
        p.write_text(text)
        assert read_check(settings) is None
