"""Kafka bandpass: record decoding, frame assembly, row mapping, storage.

No broker and no /mnt: the fixture record is the one captured on 2026-09-08 and
every store lives under tmp_path.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest

from casm_monitor.collectors import rowmap
from casm_monitor.collectors.base import CollectorContext
from casm_monitor.collectors.kafka_bp import (
    N_SUBBANDS,
    NCHAN,
    SUBBAND_NCHAN,
    AssembledFrame,
    Frame,
    KafkaBandpassCollector,
    decode_bp,
    decode_headers,
    read_latest_frame,
    to_db,
)
from casm_monitor.store import ShardReader, ShardWriter, Store

FIXTURES = Path(__file__).parent / "fixtures" / "kafka"


def fixture_record() -> tuple[bytes, dict]:
    """The captured bp body plus its decoded headers."""
    body = (FIXTURES / "casm_antenna_bp.bin").read_bytes()
    meta = json.loads((FIXTURES / "bp_headers.json").read_text())
    raw = [(k, bytes.fromhex(v)) for k, v in meta["headers_hex"].items()]
    return body, decode_headers(raw)


# -- decoding -----------------------------------------------------------
def test_headers_decode_to_scalars():
    _body, headers = fixture_record()
    assert headers["nsig"] == 66
    assert headers["npol"] == 2
    assert headers["nchan"] == SUBBAND_NCHAN
    assert headers["offset"] % SUBBAND_NCHAN == 0
    assert 0 <= headers["offset"] // SUBBAND_NCHAN < N_SUBBANDS
    assert headers["bw"] == pytest.approx(-15.625)
    assert headers["timestamp"] > 1_700_000_000  # unix seconds, not a counter


def test_decode_fixture_record():
    body, headers = fixture_record()
    block = decode_bp(body, headers)
    assert block.shape == (headers["nsig"] * headers["npol"], headers["nchan"])
    assert block.dtype == np.dtype("<f4")
    # Only pol 0 of each signal carries data in the deployed configuration.
    populated = np.flatnonzero(block.any(axis=1))
    assert populated.size > 0
    assert set(int(r) % 2 for r in populated) == {0}


def test_decode_falls_back_when_headers_disagree():
    body, headers = fixture_record()
    bad = dict(headers, nsig=7)  # 7 * 2 * 512 != the body
    block = decode_bp(body, bad)
    assert block.shape == (len(body) // 4 // SUBBAND_NCHAN, SUBBAND_NCHAN)


def test_decode_rejects_an_impossible_body():
    _body, headers = fixture_record()
    with pytest.raises(ValueError):
        decode_bp(b"\x00" * 6, dict(headers, nchan=512))


# -- frame assembly -----------------------------------------------------
def make_block(nrow: int, value: float) -> np.ndarray:
    return np.full((nrow, SUBBAND_NCHAN), value, dtype=np.float32)


def test_frame_completes_with_all_six_subbands():
    frame = Frame(ts=100.0, first_seen=0.0)
    for sub in range(N_SUBBANDS):
        frame.add(sub, make_block(4, sub + 1.0))
    assert frame.complete
    assert frame.data.shape == (4, NCHAN)
    assert frame.data[0, 0] == 1.0
    assert frame.data[0, 5 * SUBBAND_NCHAN] == 6.0


def test_frame_with_a_missing_subband_is_flagged_and_zero_filled():
    frame = Frame(ts=100.0, first_seen=0.0)
    for sub in (0, 1, 2, 4, 5):
        frame.add(sub, make_block(4, 1.0))
    assert not frame.complete
    assert frame.subbands_ok == [True, True, True, False, True, True]
    missing = frame.data[:, 3 * SUBBAND_NCHAN : 4 * SUBBAND_NCHAN]
    assert not missing.any()


def test_frame_refuses_a_row_count_change_mid_frame():
    frame = Frame(ts=1.0, first_seen=0.0)
    frame.add(0, make_block(4, 1.0))
    with pytest.raises(ValueError):
        frame.add(1, make_block(6, 1.0))


class FakeRecord:
    def __init__(self, headers, value, offset=0, timestamp=0):
        self.headers = headers
        self.value = value
        self.offset = offset
        self.timestamp = timestamp


def raw_headers(subband: int, ts: int, nsig: int = 3, npol: int = 1, nchan: int = SUBBAND_NCHAN):
    import struct

    return [
        ("offset", struct.pack("<i", subband * SUBBAND_NCHAN)),
        ("nsig", struct.pack("<i", nsig)),
        ("npol", struct.pack("<i", npol)),
        ("nchan", struct.pack("<i", nchan)),
        ("timestamp", struct.pack("<q", ts)),
    ]


def context(settings) -> CollectorContext:
    store = Store(settings.db_path, store_root=settings.store_root)
    return CollectorContext(
        settings=settings, store=store, shards=ShardWriter(store, settings.shards_root)
    )


def test_collector_assembles_a_frame_and_mirrors_it(settings):
    ctx = context(settings)
    collector = KafkaBandpassCollector(settings)
    body = np.tile(np.arange(SUBBAND_NCHAN, dtype=np.float32) + 1.0, (3, 1))
    body[1] = 0.0  # a dead row: never part of the stored set
    for sub in range(N_SUBBANDS):
        collector._ingest_bp(ctx, FakeRecord(raw_headers(sub, 1000), body.tobytes()))
    frame = collector._latest
    assert frame is not None
    assert frame.ts == 1000.0
    assert frame.rows == [0, 2]
    assert frame.subbands_ok == [True] * N_SUBBANDS
    mirrored = read_latest_frame(settings)
    assert mirrored["rows"] == [0, 2]
    assert mirrored["power_db"].shape == (2, NCHAN)
    assert mirrored["power_db"][0, 0] == pytest.approx(to_db(np.array([1.0]))[0], abs=1e-3)
    ctx.store.close()


def test_incomplete_frame_is_emitted_after_the_timeout(settings, monkeypatch):
    ctx = context(settings)
    collector = KafkaBandpassCollector(settings)
    body = np.ones((3, SUBBAND_NCHAN), dtype=np.float32)
    for sub in (0, 1, 2, 4, 5):
        collector._ingest_bp(ctx, FakeRecord(raw_headers(sub, 2000), body.tobytes()))
    assert collector._latest is None  # still waiting for subband 3
    collector._frames[2000.0].first_seen -= 60.0
    collector._flush_expired_frames(ctx)
    frame = collector._latest
    assert frame is not None
    assert frame.subbands_ok == [True, True, True, False, True, True]
    assert not frame.power[:, 3 * SUBBAND_NCHAN : 4 * SUBBAND_NCHAN].any()
    ctx.store.close()


def test_shard_flush_writes_both_streams(settings):
    ctx = context(settings)
    collector = KafkaBandpassCollector(settings)
    body = np.tile(np.arange(SUBBAND_NCHAN, dtype=np.float32) + 1.0, (2, 1))
    for step in range(3):
        for sub in range(N_SUBBANDS):
            collector._ingest_bp(
                ctx, FakeRecord(raw_headers(sub, 3000 + 60 * step, nsig=2), body.tobytes())
            )
    collector._flush_sub(ctx)
    collector._flush_full(ctx)
    reader = ShardReader(ctx.store)
    sub_shards = reader.list("kafka_bp_sub")
    full_shards = reader.list("kafka_bp_full")
    assert len(sub_shards) == 1 and len(full_shards) == 1
    sub_array, sub_meta = reader.load(sub_shards[0])
    full_array, full_meta = reader.load(full_shards[0])
    assert sub_array.shape == (3, 2, 96)  # 3072 / 32 channels
    assert full_array.shape == (3, 2, NCHAN)
    assert sub_meta["units"] == "dB" and sub_meta["chan_avg"] == 32
    assert len(full_meta["t"]) == 3
    ctx.store.close()


def test_close_flushes_the_buffered_shards(settings):
    """An orderly shutdown (SIGTERM/stop) must not lose the samples sitting in
    the sub/full buffers waiting for their periodic flush."""
    ctx = context(settings)
    collector = KafkaBandpassCollector(settings)
    body = np.tile(np.arange(SUBBAND_NCHAN, dtype=np.float32) + 1.0, (2, 1))
    for sub in range(N_SUBBANDS):
        collector._ingest_bp(ctx, FakeRecord(raw_headers(sub, 4000, nsig=2), body.tobytes()))
    assert collector._sub_buf and collector._full_buf  # nothing flushed yet
    collector.close(ctx)
    assert collector._sub_buf == [] and collector._full_buf == {}
    reader = ShardReader(ctx.store)
    assert len(reader.list("kafka_bp_sub")) == 1
    assert len(reader.list("kafka_bp_full")) == 1
    # Idempotent / harmless with nothing buffered (e.g. a second close()).
    collector.close(ctx)
    ctx.store.close()


def test_a_row_set_change_splits_the_shards(settings):
    """Samples with different row sets never share a shard (the shape and the
    meta must agree)."""
    ctx = context(settings)
    collector = KafkaBandpassCollector(settings)
    two = np.ones((2, SUBBAND_NCHAN), dtype=np.float32)
    three = np.ones((3, SUBBAND_NCHAN), dtype=np.float32)
    for sub in range(N_SUBBANDS):
        collector._ingest_bp(ctx, FakeRecord(raw_headers(sub, 5000, nsig=2), two.tobytes()))
    for sub in range(N_SUBBANDS):
        collector._ingest_bp(ctx, FakeRecord(raw_headers(sub, 5010, nsig=3), three.tobytes()))
    collector._flush_sub(ctx)
    shards = ShardReader(ctx.store).list("kafka_bp_sub")
    assert len(shards) == 2
    assert sorted(len(s["meta"]["rows"]) for s in shards) == [2, 3]
    ctx.store.close()


# -- row mapping --------------------------------------------------------
def synthetic_bandpasses(n: int, nchan: int = NCHAN, seed: int = 7) -> np.ndarray:
    """A shared bandpass shape plus a per-input ripple, as the array shows."""
    rng = np.random.default_rng(seed)
    chan = np.linspace(0.0, 1.0, nchan)
    common = 10.0 + 3.0 * np.sin(2 * np.pi * chan)
    out = []
    for i in range(n):
        ripple = 0.8 * np.sin(2 * np.pi * (4 + 3 * i) * chan + i)
        out.append((common + ripple) * (1.0 + 0.1 * i))
    return np.asarray(out)


def test_formula_row_is_the_primary_mapping():
    assert rowmap.formula_row(12) == 24
    assert rowmap.row_packet_idx(24) == 12
    assert rowmap.row_packet_idx(25) is None  # pol 1, never populated


def test_formula_mapping_covers_every_wired_input(tmp_path):
    csv_path = tmp_path / "layout.csv"
    csv_path.write_text(
        "antenna,snap,adc,packet_idx,functional\n"
        "1,0,0,0,1\n"
        "2,0,1,1,0\n"
        "3,0,2,2,1\n"
    )
    mapping = rowmap.formula_mapping(rowmap.read_layout(csv_path))
    assert set(mapping) == {0, 4}  # 2*0, 2*2 -- packet_idx 1 is not functional
    assert mapping[4] == {"packet_idx": 2, "corr": None, "runner_up": None, "status": "formula"}


def test_validate_rows_verifies_a_matching_shape():
    freq = rowmap.freq_axis_mhz()
    truth = synthetic_bandpasses(6)
    inputs = [0, 3, 7, 11, 13, 21]
    rows = [rowmap.formula_row(p) for p in inputs]
    # The row's measured shape really is its own formula input's, just
    # slightly noisier -- validation should agree with every row.
    mapping = rowmap.validate_rows(rows, truth * 1.05, inputs, truth, freq)
    assert {row: item["packet_idx"] for row, item in mapping.items()} == dict(zip(rows, inputs))
    assert all(item["status"] == "formula+verified" for item in mapping.values())


def test_validate_rows_flags_a_mismatch_without_changing_the_assignment():
    freq = rowmap.freq_axis_mhz()
    truth = synthetic_bandpasses(4)
    inputs = [0, 1, 2, 3]
    row = rowmap.formula_row(0)  # formula says this row belongs to packet_idx 0
    # ...but its measured data is really input 1's shape.
    mapping = rowmap.validate_rows([row], truth[1:2], inputs, truth, freq)
    assert mapping[row]["status"] == "mismatch"
    assert mapping[row]["packet_idx"] == 0  # the formula's answer, unchanged


def test_validate_rows_skips_rows_outside_the_formula():
    freq = rowmap.freq_axis_mhz()
    truth = synthetic_bandpasses(2)
    # row 1 is odd (pol 1, never populated); row 99 is not 2x any known input.
    mapping = rowmap.validate_rows([1, 99], truth, [0, 1], truth, freq)
    assert mapping == {}


def test_row_mapping_round_trips_through_the_store(store):
    mapping = {
        0: {"packet_idx": 0, "corr": 0.99, "runner_up": 0.5, "status": "formula+verified"},
        4: {"packet_idx": 2, "corr": 0.4, "runner_up": 0.91, "status": "mismatch"},
    }
    rowmap.save_row_map(store, mapping, {"obs": "x"}, ts=1000.0)
    loaded = rowmap.load_row_map(store)
    assert loaded[0]["packet_idx"] == 0 and loaded[0]["status"] == "formula+verified"
    assert loaded[4]["status"] == "mismatch" and loaded[4]["packet_idx"] == 2
    assert loaded[0]["source"]["obs"] == "x"
    assert rowmap.assignment(loaded) == {0: 0, 4: 2}


def test_current_mapping_overlays_validation_on_the_formula(tmp_path, store):
    csv_path = tmp_path / "layout.csv"
    csv_path.write_text(
        "antenna,snap,adc,packet_idx,functional\n"
        "1,0,0,0,1\n"
        "3,0,2,2,1\n"
    )
    # No validation persisted yet: every wired input answers "formula".
    mapping = rowmap.current_mapping(store, layout_path=csv_path)
    assert mapping[0]["status"] == "formula" and mapping[0]["packet_idx"] == 0
    assert mapping[4]["status"] == "formula" and mapping[4]["packet_idx"] == 2

    # A validation pass persists a verdict for one row: it overlays, the
    # other wired input is still the bare formula default.
    rowmap.save_row_map(
        store, {0: {"packet_idx": 0, "corr": 0.9, "runner_up": 0.1, "status": "formula+verified"}},
        {"obs": "x"},
    )
    mapping = rowmap.current_mapping(store, layout_path=csv_path)
    assert mapping[0]["status"] == "formula+verified"
    assert mapping[4]["status"] == "formula"
    assert rowmap.assignment(mapping) == {0: 0, 4: 2}


def test_wired_inputs_reads_the_functional_column(tmp_path):
    csv_path = tmp_path / "layout.csv"
    csv_path.write_text(
        "antenna,snap,adc,packet_idx,functional,include_in_beamforming,row,col,snap_ip,slot\n"
        "1,0,0,0,1,1,N21,E1,192.168.120.52,A\n"
        "2,0,1,1,0,0,,,,\n"
        "3,0,2,2,1,0,N21,E4,192.168.120.52,A\n"
    )
    assert rowmap.wired_inputs(rowmap.read_layout(csv_path)) == [0, 2]


def test_newest_complete_observation_skips_the_growing_file(tmp_path):
    for idx, size in ((0, 100), (1, 100), (2, 40)):
        (tmp_path / f"2026-09-04-16:43:47.dat.{idx}").write_bytes(b"\x00" * size)
    (tmp_path / "2026-09-01-00:00:00.dat.0").write_bytes(b"\x00" * 100)
    obs, index = rowmap.newest_complete_observation(tmp_path)
    assert obs == "2026-09-04-16:43:47"
    assert index == 1


# -- nightly median -----------------------------------------------------
def test_night_median_reduces_the_window(settings, tmp_path, monkeypatch):
    # A tmp layout keeps the formula deterministic (row 0 = packet_idx 0),
    # independent of whatever real layout happens to be on this host.
    csv_path = tmp_path / "layout.csv"
    csv_path.write_text("antenna,snap,adc,packet_idx,functional\n1,0,0,0,1\n")
    monkeypatch.setattr(rowmap, "LAYOUT_CSV", csv_path)

    ctx = context(settings)
    collector = KafkaBandpassCollector(settings)
    collector._rows = [0, 2]
    t0 = time.time() - 7200.0
    times = [t0 + 60.0 * i for i in range(5)]
    array = np.stack([np.full((2, NCHAN), 3.0 + i, dtype=np.float32) for i in range(5)])
    ctx.shards.write(
        "kafka_bp_full",
        array,
        t0=times[0],
        t1=times[-1],
        meta={"rows": [0, 2], "t": times, "units": "dB"},
    )
    rowmap.save_row_map(
        ctx.store,
        {0: {"packet_idx": 0, "corr": 0.99, "runner_up": 0.1, "status": "formula+verified"}},
        {"obs": "x"},
    )
    collector._night_median(ctx, "2026-09-08", times[0] - 1, times[-1] + 1)
    reader = ShardReader(ctx.store)
    shards = reader.list("kafka_bp_night")
    assert len(shards) == 1
    median, meta = reader.load(shards[0])
    assert median.shape == (2, NCHAN)
    assert median[0, 0] == pytest.approx(5.0)  # median of 3..7
    assert meta["n_samples"] == 5
    tagged = ctx.store.query(
        "SELECT value FROM scalars WHERE name = 'snaps.night_median_db' "
        "AND json_extract(tags, '$.packet_idx') = 0"
    )
    assert len(tagged) == 1
    ctx.store.close()
