import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from casm_monitor.config import Settings
from casm_monitor.web import commissioning as c
from casm_monitor.web.source_history import attempt_rows, images_for, source_history


@pytest.fixture
def setup(tmp_path, monkeypatch):
    layout = tmp_path / 'layout.csv'
    layout.write_text('antenna,x,y,z,functional,include_in_beamforming\n9,0,0,0,1,1\n10,0,10,0,1,0\n11,0,20,0,0,0\n')
    settings = Settings(store_root=tmp_path / 'production', observation_cache_root=tmp_path / 'preview', snap_layout_csv=layout)
    def validate(body, setting):
        assert c.layout_info(setting.layout_csv)['antennas'] == [9, 10]
        return dict(source='sun', tag=body['tag'], source_window=body['source_window'], static_window=None,
                    antennas=body['antennas'], ref_ant=9, prev_cal_path=None)
    monkeypatch.setattr(c, 'validate', validate)
    body = dict(reviewed=True, source='sun', tag='test_build', source_window=['2026-09-13 19:00:00', '2026-09-13 20:00:00'],
                static_window=None, antennas=[9, 10], ref_ant=9)
    return settings, body


def test_stage_isolated_and_reviewed(setup):
    settings, body = setup
    before = settings.layout_csv.read_bytes()
    result = c.stage(settings, body)
    assert result['state'] == 'staged'
    assert result['recipe']['grid_mode'] == 'exact'
    assert 'bf_weights_generator.make_cal_and_weights' in result['command']
    assert not result['deployment_enabled']
    assert settings.layout_csv.read_bytes() == before
    assert not settings.store_root.exists()
    assert c.layout_info(result['recipe']['layout_csv'])['antennas'] == [9, 10]


def test_stage_rejects_bad_membership_and_no_review(setup):
    settings, body = setup
    for updates in ({'reviewed': False}, {'antennas': [9, 11]}, {'antennas': [9, 9]}, {'antennas': [True]}):
        with pytest.raises(HTTPException):
            c.stage(settings, {**body, **updates})
    with pytest.raises(HTTPException):
        c.stage(dataclasses.replace(settings, observation_cache_root=settings.store_root / 'preview'), body)


def test_start_pins_inputs_and_requires_confirmation(setup):
    settings, body = setup
    record = c.stage(settings, body)
    with pytest.raises(HTTPException) as error:
        c.start(settings, record['id'], {'confirm_id': 'wrong'})
    assert error.value.status_code == 400
    (Path(record['recipe']['out_dir']) / 'recipe.json').write_text('{}')
    with pytest.raises(HTTPException) as error:
        c.start(settings, record['id'], {'confirm_id': record['id']})
    assert error.value.status_code == 409


def test_execution_uses_canonical_limited_cli_only(setup, monkeypatch):
    settings, body = setup
    record = c.stage(settings, body)
    path = Path(record['recipe']['out_dir'])
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert kwargs['cwd'] == path
        assert kwargs['env']['MPLBACKEND'] == 'Agg'
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(c.subprocess, 'run', run)
    assert c._lock.acquire(False)
    c.execute(path)
    assert c.build_view(path)['state'] == 'review_required'
    assert calls[0][0] == '/usr/bin/timeout'
    assert '/usr/bin/prlimit' in calls[0]
    assert 'bf_weights_generator.make_cal_and_weights' in calls[0]
    assert not any('deploy' in arg for arg in calls[0])
    assert not c._lock.locked()


def test_history_retains_nulls_and_retractions_and_bounded_artifacts(tmp_path):
    archive = tmp_path / 'archive'
    archive.mkdir()
    (archive / 'b0329_pdmp.png').write_bytes(b'png')
    outside = tmp_path / 'private.png'
    outside.write_bytes(b'secret')
    (archive / 'escape_pdmp.png').symlink_to(outside)
    # Test root deliberately uses a real allowed CASM-style path via parser override below.
    ledger = ('## B0329+54 attempt table\n'
              '| 2026-09-01 | no archive | test | NON-DETECTION 5.55, width 11 ms |\n'
              '| 2026-09-02 | no archive | RETRACTED gain | NON-DETECTION 7.23 |\n'
              '## Fold recipe\n')
    (tmp_path / 'detections.md').write_text(ledger)
    rows = attempt_rows(tmp_path)
    assert len(rows) == 2
    assert rows[0]['contains_retraction']
    assert rows[1]['status'] == 'non_detection'
    assert rows[1]['snr'] is None
    rows[0]['roots'] = [str(archive)]
    assert [p.name for p in images_for(rows[0], tmp_path)[0]] == ['b0329_pdmp.png']
    assert len(source_history('B0329', tmp_path)['sources'][0]['attempts']) == 2
    assert source_history('unknown', tmp_path)['state'] == 'no_match'


def test_artifact_allowlist_and_traversal(setup):
    settings, body = setup
    result = c.stage(settings, body)
    path = Path(result['recipe']['out_dir'])
    (path / 'figs').mkdir()
    (path / 'figs' / 'phase.png').write_bytes(b'png')
    (path / 'figs' / 'secret.png').symlink_to(settings.layout_csv)
    assert [a['name'] for a in c.artifacts(path)] == ['phase.png']
    with pytest.raises(HTTPException):
        c.build_path(settings, '../production')


def test_real_admission_reuses_reviewed_layout_and_rejects_future(setup, monkeypatch):
    from casm_monitor.jobs import cal_build
    settings, body = setup
    monkeypatch.setattr(c, 'validate', cal_build.validate)
    monkeypatch.setattr(cal_build, 'deployed_product', lambda settings: {})
    monkeypatch.setattr(cal_build, 'ib_generator_refusal', lambda *args: None)
    now = datetime.now(timezone.utc)
    body['source_window'] = [(now - timedelta(hours=2)).isoformat().replace('+00:00', 'Z'),
                             (now - timedelta(hours=1)).isoformat().replace('+00:00', 'Z')]
    record = c.stage(settings, body)
    assert record['reviewed_antennas'] == [9, 10]
    body['source_window'] = [(now + timedelta(hours=1)).isoformat().replace('+00:00', 'Z'),
                             (now + timedelta(hours=2)).isoformat().replace('+00:00', 'Z')]
    with pytest.raises(HTTPException) as error:
        c.stage(settings, body)
    assert 'future' in error.value.detail


def test_refuse_symlink_root_and_report_lost_supervisor(setup):
    settings, body = setup
    settings.observation_cache_root.mkdir()
    settings.store_root.mkdir()
    (settings.observation_cache_root / 'commissioning').symlink_to(settings.store_root, target_is_directory=True)
    with pytest.raises(HTTPException) as error:
        c.local_root(settings)
    assert error.value.status_code == 503
    (settings.observation_cache_root / 'commissioning').unlink()
    record = c.stage(settings, body)
    path = Path(record['recipe']['out_dir'])
    record.update(state='running', supervisor_pid=0)
    c.save(path, record)
    view = c.build_view(path)
    assert view['state'] == 'interrupted'
    assert view['recorded_state'] == 'running'
    assert 'may still hold' in view['warning']
    assert json.loads((path / 'state.json').read_text())['state'] == 'running'
