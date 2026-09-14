import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from casm_monitor.web import calibration_reference as cr


def fixture(tmp_path, monkeypatch, **overrides):
    cal = tmp_path / 'cal_test.h5'
    cal.write_bytes(b'identity only, no HDF5 read')
    params = dict(out_dir=str(tmp_path), tag='test', cal_source='sun', cal_path=None,
                  source_window=['2026-09-03 19:22:00', '2026-09-03 20:22:00'],
                  antennas=[9, 19], layout_csv='recorded-layout.csv')
    params.update(overrides)
    report = tmp_path / 'report_test.json'
    report.write_text(json.dumps(dict(params=params, antennas=[9, 19])))
    monkeypatch.setattr(cr, 'deployed_product', lambda s: dict(cal_file=str(cal), weights_file='recorded-weights.h5',
                                                             product_id='recorded-id', date_deployed='2026-09-04'))
    return cal, report


def test_reference_window_comes_from_bound_report(tmp_path, monkeypatch):
    cal, report = fixture(tmp_path, monkeypatch)
    result = cr.references(None, '2026-09-13', now=datetime(2026, 9, 14, tzinfo=timezone.utc))
    r = result['references'][0]
    assert result['state'] == 'ready' and r['can_render']
    assert r['source_window'] == ['2026-09-03T19:22:00+00:00', '2026-09-03T20:22:00+00:00']
    assert r['comparison_window'] == ['2026-09-13T19:22:00+00:00', '2026-09-13T20:22:00+00:00']
    assert r['calibration']['path'] == str(cal)
    assert r['report']['path'] == str(report)
    assert r['antennas'] == [9, 19]
    assert len(r['report_sha256']) == 64


def test_no_filename_only_or_wrong_report_association(tmp_path, monkeypatch):
    fixture(tmp_path, monkeypatch, tag='other-calibration')
    result = cr.references(None, '2026-09-13')
    assert result['state'] == 'unavailable'
    assert 'not inferred from the filename' in result['reason']


def test_future_window_is_not_silently_shortened(tmp_path, monkeypatch):
    fixture(tmp_path, monkeypatch)
    result = cr.references(None, '2026-09-13', now=datetime(2026, 9, 13, 18, tzinfo=timezone.utc))
    r = result['references'][0]
    assert not r['can_render']
    assert r['comparison_window'][1] == '2026-09-13T20:22:00+00:00'
    assert 'has not completed' in r['reason']


def test_day_matching_preserves_local_clock_across_dst(tmp_path, monkeypatch):
    fixture(tmp_path, monkeypatch, source_window=['2026-10-31 19:22:00', '2026-10-31 20:22:00'])
    result = cr.references(None, '2026-11-02', now=datetime(2026, 11, 3, tzinfo=timezone.utc))
    r = result['references'][0]
    assert r['comparison_window'] == ['2026-11-02T20:22:00+00:00', '2026-11-02T21:22:00+00:00']
    assert r['can_render']


def test_invalid_date_and_overbudget_recipe_are_refused(tmp_path, monkeypatch):
    fixture(tmp_path, monkeypatch, source_window=['2026-09-03 18:22:00', '2026-09-03 20:22:00'])
    assert cr.references(None, '2026-09-13')['state'] == 'unavailable'
    with pytest.raises(HTTPException) as error:
        cr.references(None, 'tomorrow')
    assert error.value.status_code == 400


def guarded_reference(monkeypatch):
    from casm_monitor.web import science
    reference = dict(id='1' * 32, comparison_window=['2026-09-13T19:22:00+00:00', '2026-09-13T20:22:00+00:00'],
                     source_window=['2026-09-03T19:22:00+00:00', '2026-09-03T20:22:00+00:00'],
                     antennas=[9, 19], can_render=True, reason=None)
    monkeypatch.setattr(cr, 'references', lambda *a: dict(references=[reference]))
    def no_data(*args, **kwargs):
        raise AssertionError('Rejected comparison must not reach visibility access')
    monkeypatch.setattr(science, 'VisStore', no_data)
    monkeypatch.setattr(science, 'load_selection', no_data)
    body = dict(pairs=[(8, 18)], t0=reference['comparison_window'][0], t1=reference['comparison_window'][1],
                compare_t0=reference['source_window'][0], compare_t1=reference['source_window'][1],
                kind='phase_spectrum', reference='sun', resolution='recorded', calibration_reference_id=reference['id'])
    return science, reference, body


@pytest.mark.parametrize('change,code', [
    ({'calibration_reference_id': '2' * 32}, 409),
    ({'reference': 'raw'}, 400),
    ({'kind': 'amplitude_spectrum'}, 400),
    ({'resolution': 'avg8'}, 400),
    ({'compare_t0': '2026-09-03T19:23:00Z'}, 400),
    ({'t1': '2026-09-13T20:21:00Z'}, 400),
])
def test_forged_processing_and_window_rejected_before_read(monkeypatch, change, code):
    science, _, body = guarded_reference(monkeypatch)
    monkeypatch.setattr(science, 'select_layout', lambda *a: pytest.fail('Reference refusal must precede layout/data access'))
    with pytest.raises(HTTPException) as error:
        science.render_product(None, None, science.ScienceRequest(**(body | change)))
    assert error.value.status_code == code


def test_stale_reference_and_unfinished_window_rejected_before_read(monkeypatch):
    science, reference, body = guarded_reference(monkeypatch)
    request = science.ScienceRequest(**body)
    # Reload has changed the report/calibration identity, even though UI still has the old selection.
    reference['id'] = '3' * 32
    with pytest.raises(HTTPException) as error:
        science.render_product(None, None, request)
    assert error.value.status_code == 409
    reference['id'] = body['calibration_reference_id']
    reference.update(can_render=False, reason='Solar window has not completed')
    with pytest.raises(HTTPException) as error:
        science.render_product(None, None, request)
    assert error.value.status_code == 400
    assert 'not completed' in error.value.detail


def test_reference_antenna_membership_checked_before_read(monkeypatch):
    science, _, body = guarded_reference(monkeypatch)
    monkeypatch.setattr(science, 'select_layout', lambda *a: dict(path='fixture'))
    monkeypatch.setattr(science, 'geometry', lambda *a: ([dict(packet_idx=8, antenna=9, label='A'),
                                                       dict(packet_idx=18, antenna=99, label='B')], []))
    with pytest.raises(HTTPException) as error:
        science.render_product(None, None, science.ScienceRequest(**body))
    assert error.value.status_code == 400
    assert 'antenna set' in error.value.detail
