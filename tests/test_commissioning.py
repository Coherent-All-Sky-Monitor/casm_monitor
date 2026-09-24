import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from casm_monitor.config import Settings
from casm_monitor.web import commissioning as c
from casm_monitor.web import source_history as sh
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


def test_history_status_detection_word_and_filename_trap(tmp_path):
    ledger = ('## B0329+54 attempt table\n'
              '| 2026-07-01 | no archive | test | FIRST DETECTION 9.2 sigma pdmp, beam 2 |\n'
              '| 2026-07-02 | no archive | test | weak, CONTESTED |\n'
              '| 2026-07-03 | no archive | test | Plot `out/cb_detection.png` only, no claim in prose |\n'
              '| 2026-07-04 | no archive | test | DETECTIONS on all 13 folds |\n'
              '## Fold recipe\n')
    (tmp_path / 'detections.md').write_text(ledger)
    rows = {r['date']: r for r in attempt_rows(tmp_path)}
    assert rows['2026-07-01']['status'] == 'detection'
    assert rows['2026-07-02']['status'] == 'contested'
    # the word only appears inside a backticked filename, not a prose claim
    assert rows['2026-07-03']['status'] == 'recorded_attempt'
    assert rows['2026-07-04']['status'] == 'detection'


def test_referenced_pngs_orders_and_expands_braces():
    text = ('Plots `fold_lock/b470_s02_lock_pdmp.png` and `fold_lock/b377_s02_lock_pdmp.{png,log}` '
            'and `evidence/x/{a.png,b.txt,c.png}`')
    assert sh.referenced_pngs(text) == ['b470_s02_lock_pdmp.png', 'b377_s02_lock_pdmp.png', 'a.png', 'c.png']


def test_legacy_detection_review_applies_only_to_unchanged_row(tmp_path, monkeypatch):
    import hashlib
    line = '| 2026-05-25 | no archive | test | 14.40 at-par at alpha=-1 |'
    monkeypatch.setattr(sh, 'LEGACY_DETECTIONS', {hashlib.sha256(line.encode()).hexdigest()[:16]})
    path = tmp_path / 'detections.md'
    path.write_text('## B0329+54 attempt table\n' + line + '\n## Fold recipe\n')
    assert attempt_rows(tmp_path)[0]['status'] == 'detection'
    path.write_text(path.read_text().replace('14.40', '14.41'))
    assert attempt_rows(tmp_path)[0]['status'] == 'recorded_attempt'
    path.write_text(path.read_text().replace('14.41 at-par at alpha=-1', 'NON-DETECTION 14.41'))
    assert attempt_rows(tmp_path)[0]['status'] == 'non_detection'


def test_reviewed_pdmp_headline_does_not_create_files(tmp_path, monkeypatch):
    line = '| 2026-07-01 | none | test | DETECTION |'
    (tmp_path / 'detections.md').write_text('## B0329+54 attempt table\n' + line + '\n## Fold recipe\n')
    row = attempt_rows(tmp_path)[0]
    monkeypatch.setattr(sh, 'HEADLINE_PDMP', {row['id']: 'source_pdmp.png'})
    monkeypatch.setattr(sh, 'images_for', lambda *a: ([Path('/a/control_pdmp.png'), Path('/a/source_pdmp.png')], False))
    result = source_history('B0329', tmp_path)['sources'][0]['attempts'][0]
    assert result['artifacts'][0]['name'] == 'source_pdmp.png'
    assert result['headline_selection'] == 'reviewed_pdmp'
    assert not result['headline_from_ledger']
    monkeypatch.setattr(sh, 'images_for', lambda *a: ([], False))
    assert source_history('B0329', tmp_path)['sources'][0]['attempts'][0]['artifacts'] == []


def test_reviewed_headline_distinguishes_same_basename_and_allows_no_plot(tmp_path, monkeypatch):
    (tmp_path / 'detections.md').write_text('## B0329+54 attempt table\n| 2026-07-01 | none | test | DETECTION |\n')
    row = attempt_rows(tmp_path)[0]
    monkeypatch.setattr(sh, 'HEADLINE_PDMP', {row['id']: 'good/same_pdmp.png'})
    monkeypatch.setattr(sh, 'images_for', lambda *a: ([Path('/a/bad/same_pdmp.png'), Path('/a/good/same_pdmp.png')], False))
    result = source_history('B0329', tmp_path)['sources'][0]['attempts'][0]
    assert result['headline_artifact']['id'] == sh.hashlib.sha256(b'/a/good/same_pdmp.png').hexdigest()[:20]
    monkeypatch.setattr(sh, 'HEADLINE_PDMP', {row['id']: None})
    result = source_history('B0329', tmp_path)['sources'][0]['attempts'][0]
    assert result['headline_artifact'] is None and len(result['artifacts']) == 2


def test_discovery_admits_reviewed_fold_without_pdmp_in_filename(tmp_path, monkeypatch):
    (tmp_path / 'source_DMlocked.png').write_bytes(b'png')
    (tmp_path / 'unrelated.png').write_bytes(b'png')
    monkeypatch.setattr(sh, 'HEADLINE_PDMP', {'row': 'source_DMlocked.png'})
    paths, partial = images_for(dict(id='row', roots=[str(tmp_path)], date='2026-07-01'), tmp_path)
    assert [p.name for p in paths] == ['source_DMlocked.png'] and not partial


def test_promote_headline_artifacts_reorders_borrows_and_flags(tmp_path):
    rows = [
        dict(date='2026-07-10', outcome='DETECTION `fold_lock/bhead_pdmp.png` seen', directory='no dirs',
             artifacts=[dict(id='1', name='aaa_pdmp.png', url='/a'), dict(id='2', name='bhead_pdmp.png', url='/b')]),
        dict(date='2026-07-10', outcome='non-detection retry', directory='folds `fold_lock/bhead_pdmp.{png,log}`',
             artifacts=[dict(id='3', name='other_pdmp.png', url='/c')]),
        dict(date='2026-07-11', outcome='no reference here', directory='none',
             artifacts=[dict(id='4', name='zzz_pdmp.png', url='/d')]),
    ]
    sh._promote_headline_artifacts(rows)
    # named basename promoted to front even though scan order put it second
    assert rows[0]['artifacts'][0]['name'] == 'bhead_pdmp.png'
    assert rows[0]['headline_from_ledger'] is True
    # row 2 has no local match; borrows the same-date row's artifact (same url)
    assert rows[1]['artifacts'][0]['url'] == '/b'
    assert rows[1]['headline_from_ledger'] is True
    # row 3 names nothing, so it keeps its own first artifact and is flagged false
    assert rows[2]['artifacts'][0]['name'] == 'zzz_pdmp.png'
    assert rows[2]['headline_from_ledger'] is False


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
