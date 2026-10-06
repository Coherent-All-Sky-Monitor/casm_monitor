"""Deployment defaults follow audited uploads, never an unsent workspace build."""
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from casm_monitor import cal_defaults as cd
from casm_monitor import deployment_metadata as dm
from casm_monitor.store import Store
from casm_monitor.web.calibration_reference import recipe_for


def environment(settings, tmp_path, monkeypatch):
    settings = replace(settings, registry_dir=tmp_path/'registry',
                       observation_cache_root=tmp_path/'observation')
    settings.registry_dir.mkdir()
    (settings.registry_dir/'products').mkdir()
    weights = tmp_path/'new.h5'
    cal = tmp_path/'new_cal.h5'
    cal.write_bytes(b'calibration')
    (settings.registry_dir/'products'/'new.json').write_text(json.dumps(dict(
        product_id='new', h5_path=str(weights), meta=dict(scale=999))))
    monkeypatch.setattr(cd, 'read_last_ledger_row', lambda p: dict(
        weights_file='/old/cb.h5 (+ ib.h5)', cal_file='/old/cal.h5',
        notes='--scale 8064 --ib-scale 32'))
    stamp = 1791000000
    events = [dict(stream=s, product_id='new', source='upload', utc=datetime.fromtimestamp(stamp, timezone.utc).isoformat()) for s in range(6)]
    (settings.registry_dir/'live_events.jsonl').write_text('\n'.join(map(json.dumps, events)))
    snapshot = dict(kind='cb', streams=list(range(6)), weights_h5=str(weights),
                    ib_h5='/unselected/ib.h5', cal_h5=str(cal), cal_sha256=dm.digest(cal),
                    scale=4096, ib_scale=999)
    return settings, snapshot, stamp, events


def receipt(settings, snapshot, stamp, *, state='done', pid='new', save_defaults=False):
    path = settings.observation_cache_root/'calibration'/'monitor.sqlite'
    store = Store(path, store_root=path.parent)
    try:
        store.add_upload('test', ts=stamp-1, state=state, exit_code=0, registry='ok', product_id=pid,
                         save_defaults=save_defaults,
                         hashes=dict(deployment=snapshot, completed_utc_unix=stamp+1))
    finally:
        store.close()


def test_only_successful_upload_selects_new_reference(settings, tmp_path, monkeypatch):
    settings, snapshot, stamp, events = environment(settings, tmp_path, monkeypatch)
    receipt(settings, snapshot, stamp, state='failed')
    result = cd.deployed_product(settings)
    assert result['cal_file'] is None and result['scale'] is None
    receipt(settings, snapshot, stamp)
    result = cd.deployed_product(settings)
    assert result['cal_file'] == snapshot['cal_h5']
    assert result['reference_source'] == 'successful_upload'
    assert result['scale'] == 4096  # not immutable product.meta.scale=999
    assert result['ib_scale'] == 32 and result['ib_file'] == '/old/ib.h5'
    assert result['ledger_matches'] is False
    # A subsequent failed attempt cannot supersede the successful receipt.
    receipt(settings, dict(snapshot, scale=64), stamp+100, state='failed')
    assert cd.deployed_product(settings)['scale'] == 4096
    # IB-only does not change CB membership, calibration, or SCALE.
    receipt(settings, dict(snapshot, kind='ib', ib_scale=16, ib_h5='/new/ib.h5'), stamp+200, pid=None)
    result = cd.deployed_product(settings)
    assert (result['scale'], result['ib_scale'], result['ib_file']) == (4096, 16, '/new/ib.h5')
    assert result['cal_file'] == snapshot['cal_h5']
    # Changed calibration bytes cannot silently become the current reference.
    from pathlib import Path
    Path(snapshot['cal_h5']).write_bytes(b'replaced')
    assert cd.deployed_product(settings)['cal_file'] is None


def test_mixed_streams_and_scale_receipts_are_not_one_configuration(settings, tmp_path, monkeypatch):
    settings, snapshot, stamp, events = environment(settings, tmp_path, monkeypatch)
    receipt(settings, dict(snapshot, streams=[0, 1, 2]), stamp)
    assert cd.deployed_product(settings)['scale'] is None
    receipt(settings, dict(snapshot, streams=[3, 4, 5], scale=8192), stamp)
    assert cd.deployed_product(settings)['scale'] is None
    events[5]['product_id'] = 'other'
    (settings.registry_dir/'live_events.jsonl').write_text('\n'.join(map(json.dumps, events)))
    result = cd.deployed_product(settings)
    assert result['product_id'] is None and result['cal_file'] is None


def test_saved_defaults_reload_retains_scale_and_deployment_date(settings, tmp_path, monkeypatch):
    settings, snapshot, stamp, events = environment(settings, tmp_path, monkeypatch)
    receipt(settings, snapshot, stamp, save_defaults=True)
    before = cd.deployed_product(settings)
    receipt(settings, dict(snapshot, scale=128), stamp+100)
    for event in events:
        event.update(source='defaults', utc=datetime.fromtimestamp(stamp+3600, timezone.utc).isoformat())
    (settings.registry_dir/'live_events.jsonl').write_text('\n'.join(map(json.dumps, events)))
    after = cd.deployed_product(settings)
    assert after['scale'] == before['scale'] == 4096
    assert after['cal_file'] == before['cal_file']
    assert after['date_deployed'] == before['date_deployed']
    assert after['last_load_utc'] != before['last_load_utc']


def test_ledger_fallback_after_unmatched_cli_load_is_labelled(settings, tmp_path, monkeypatch):
    settings, snapshot, stamp, events = environment(settings, tmp_path, monkeypatch)
    receipt(settings, snapshot, stamp)
    monkeypatch.setattr(cd, 'read_last_ledger_row', lambda p: dict(
        weights_file=snapshot['weights_h5']+' (+ ib.h5)', cal_file=snapshot['cal_h5'],
        notes='--scale 8192 --ib-scale 32'))
    for event in events:
        event['utc'] = datetime.fromtimestamp(stamp+3600, timezone.utc).isoformat()
    (settings.registry_dir/'live_events.jsonl').write_text('\n'.join(map(json.dumps, events)))
    result = cd.deployed_product(settings)
    assert result['scale'] == 8192 and result['scale_source'] == 'ledger'


def test_monitor_fallback_registers_full_identity_for_three_streams(settings, tmp_path):
    import h5py
    import numpy as np
    from bf_weights_generator.beam_metadata import cb_payload_md5s
    from casm_t2.weights_registry import product_id_from_payloads
    from casm_monitor.jobs.deploy import ensure_registered
    settings = replace(settings, registry_dir=tmp_path/'registry')
    path = tmp_path/'weights.h5'
    with h5py.File(path, 'w') as f:
        f['weights_int8'] = np.ones((2,3072,2,1,4), dtype='i1')
        f['array_config/positions_enu'] = [[0,0,0],[3,5,0],[2,10,0],[1,20,0]]
        f['frequencies_hz'] = [437.5e6]
        f['pointings/alt_deg'] = [60.]*512
        f['pointings/az_deg'] = [180.]*512
    hashes = cb_payload_md5s(path)
    stage = dict(weights_h5=str(path), build_tag='test', scale=8064, ib_scale=32,
                 options=dict(kind='cb', streams=[0,2,4]),
                 payload_md5s={f'direct.dada.{s}':hashes[s] for s in (0,2,4)})
    result = ensure_registered(settings, stage)
    assert result['product_id'] == product_id_from_payloads(hashes)
    record = json.loads((settings.registry_dir/'products'/f"{result['product_id']}.json").read_text())
    assert len(record['stream_payload_md5']) == 6 and record['beam_fwhm_x_deg'] > 0
    events = [json.loads(line) for line in (settings.registry_dir/'live_events.jsonl').read_text().splitlines()]
    assert [event['stream'] for event in events] == [0,2,4]


def test_exported_recipe_is_hash_bound_and_survives_scratch_cleanup(tmp_path):
    root = tmp_path/'export'
    (root/'calibration').mkdir(parents=True)
    (root/'diagnostics').mkdir()
    cal = root/'calibration'/'cal_test.h5'
    cal.write_bytes(b'cal')
    report = root/'diagnostics'/'report_test.json'
    report.write_text(json.dumps(dict(params=dict(out_dir='/deleted/scratch', tag='test',
        cal_source='sun', source_window=['2026-10-04 18:00:00', '2026-10-04 19:00:00']), antennas=[9,19])))
    entries = [dict(role=role, relative=str(p.relative_to(root)), sha256=dm.digest(p), source='/deleted/scratch/'+p.name)
               for role,p in [('cal_h5', cal), ('diagnostic:solve', report)]]
    (root/'manifest.json').write_text(json.dumps(dict(state='written', files=entries)))
    recipe, bound = recipe_for(cal)
    assert recipe['antennas'] == [9,19] and bound == report
    report.write_text('{}')
    with pytest.raises(ValueError, match='changed'):
        recipe_for(cal)
