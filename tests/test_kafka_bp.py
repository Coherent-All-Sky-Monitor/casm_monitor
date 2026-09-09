"""Kafka bandpass: record decoding, frame assembly, row mapping, storage.

No broker and no /mnt: the fixture record is the one captured on 2026-09-08 and
every store lives under tmp_path.
"""

from __future__ import annotations

import json
import struct
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


def test_decode_refuses_a_body_that_disagrees_with_its_headers():
    """No guessing: a body that is not nsig*npol*nchan float32 is refused, so a
    record we do not understand cannot be reshaped into a wrong row mapping."""
    body, headers = fixture_record()
    with pytest.raises(ValueError):
        decode_bp(body, dict(headers, nsig=7))  # 7 * 2 * 512 != the body


def test_decode_rejects_an_impossible_body():
    _body, headers = fixture_record()
    with pytest.raises(ValueError):
        decode_bp(b"\x00" * 6, dict(headers, nchan=512))


def test_decode_rejects_a_non_subband_nchan():
    body, headers = fixture_record()
    with pytest.raises(ValueError):
        decode_bp(body, dict(headers, nchan=NCHAN, nsig=22, npol=1))


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
    # Not yet: the timeout has to outlast the ~68 s producer skew (150 s here).
    collector._frames[2000.0].first_seen -= 100.0
    collector._flush_expired_frames(ctx)
    assert collector._latest is None
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


def test_records_without_a_timestamp_header_are_dropped_and_counted(settings):
    """No CreateTime fallback: without the header there is no frame identity
    the other five producers would agree on."""
    ctx = context(settings)
    collector = KafkaBandpassCollector(settings)
    body = np.ones((2, SUBBAND_NCHAN), dtype=np.float32)
    headers = [h for h in raw_headers(0, 6000, nsig=2) if h[0] != "timestamp"]
    collector._ingest_bp(ctx, FakeRecord(headers, body.tobytes(), offset=11, timestamp=6_000_000))
    assert collector._frames == {}
    assert collector._latest is None
    assert collector._dropped == {"no_timestamp": 1}
    ctx.store.close()


def test_duplicate_and_late_records_are_dropped(settings):
    ctx = context(settings)
    collector = KafkaBandpassCollector(settings)
    body = np.ones((2, SUBBAND_NCHAN), dtype=np.float32)
    collector._ingest_bp(ctx, FakeRecord(raw_headers(0, 7000, nsig=2), body.tobytes(), offset=0))
    # Same (timestamp, offset) twice: the second copy adds nothing.
    collector._ingest_bp(ctx, FakeRecord(raw_headers(0, 7000, nsig=2), body.tobytes(), offset=1))
    assert collector._dropped == {"duplicate_subband": 1}
    for sub in range(1, N_SUBBANDS):
        collector._ingest_bp(
            ctx, FakeRecord(raw_headers(sub, 7000, nsig=2), body.tobytes(), offset=sub + 1)
        )
    assert collector._latest is not None and collector._latest.ts == 7000.0
    n_sub = len(collector._sub_buf)

    # A straggler for the frame that has already been emitted.
    collector._ingest_bp(ctx, FakeRecord(raw_headers(2, 7000, nsig=2), body.tobytes(), offset=99))
    assert collector._dropped["late_record"] == 1
    assert len(collector._sub_buf) == n_sub
    ctx.store.close()


def test_latest_only_moves_forward(settings):
    ctx = context(settings)
    collector = KafkaBandpassCollector(settings)
    body = np.ones((2, SUBBAND_NCHAN), dtype=np.float32)
    for sub in range(N_SUBBANDS):
        collector._ingest_bp(ctx, FakeRecord(raw_headers(sub, 8010, nsig=2), body.tobytes()))
    assert collector._latest.ts == 8010.0
    # An older frame completes afterwards: it is stored, but it is not "live".
    for sub in range(N_SUBBANDS):
        collector._ingest_bp(ctx, FakeRecord(raw_headers(sub, 8000, nsig=2), body.tobytes()))
    assert collector._latest.ts == 8010.0
    assert sorted(s.ts for s in collector._sub_buf) == [8000.0, 8010.0]
    assert read_latest_frame(settings)["ts"] == 8010.0
    ctx.store.close()


def test_records_with_a_bad_offset_or_shape_are_refused(settings):
    ctx = context(settings)
    collector = KafkaBandpassCollector(settings)
    body = np.ones((2, SUBBAND_NCHAN), dtype=np.float32)
    bad_offset = [
        (k, v) for k, v in raw_headers(0, 9000, nsig=2) if k != "offset"
    ] + [("offset", struct.pack("<i", 700))]
    with pytest.raises(ValueError):
        collector._ingest_bp(ctx, FakeRecord(bad_offset, body.tobytes()))
    with pytest.raises(ValueError):
        collector._ingest_bp(
            ctx, FakeRecord(raw_headers(0, 9000, nsig=3), body.tobytes())  # body is 2 rows
        )
    assert collector._frames == {}
    ctx.store.close()


def test_watermark_waits_for_the_shard_and_a_replay_is_idempotent(settings):
    """The Kafka read position may not pass a record whose frame is still only
    in a buffer, and re-reading it after a restart must not duplicate history."""
    ctx = context(settings)
    collector = KafkaBandpassCollector(settings)
    body = np.ones((2, SUBBAND_NCHAN), dtype=np.float32)
    for sub in range(N_SUBBANDS):
        collector._ingest_bp(
            ctx,
            FakeRecord(raw_headers(sub, 10_000, nsig=2), body.tobytes(), offset=100 + sub),
        )
    collector._offset_seen["casm_antenna_bp"] = 105
    collector._advance_watermarks(ctx)
    # Buffered but not written: the watermark stops one below the frame's
    # lowest record (100), not at the last record consumed (105).
    assert int(ctx.store.get_watermark("kafka_bp", "offset:casm_antenna_bp")) == 99

    collector._flush_sub(ctx)
    collector._advance_watermarks(ctx)
    # The 60 s buffer still holds the same frame, so still nothing to advance.
    assert int(ctx.store.get_watermark("kafka_bp", "offset:casm_antenna_bp")) == 99
    collector._flush_full(ctx)
    collector._advance_watermarks(ctx)
    assert int(ctx.store.get_watermark("kafka_bp", "offset:casm_antenna_bp")) == 105

    # A replay of the very same records (what a restart from the watermark
    # does) re-writes the same (stream, t0) shards, which are skipped.
    before = {s: len(ShardReader(ctx.store).list(s)) for s in ("kafka_bp_sub", "kafka_bp_full")}
    replay = KafkaBandpassCollector(settings)
    for sub in range(N_SUBBANDS):
        replay._ingest_bp(
            ctx,
            FakeRecord(raw_headers(sub, 10_000, nsig=2), body.tobytes(), offset=100 + sub),
        )
    replay._flush_sub(ctx)
    replay._flush_full(ctx)
    after = {s: len(ShardReader(ctx.store).list(s)) for s in ("kafka_bp_sub", "kafka_bp_full")}
    assert after == before

    # ...and also when the replay's buffer boundary differs, so the shard would
    # get a new t0: the samples are already published and are not rewritten.
    replay2 = KafkaBandpassCollector(settings)
    for ts in (9_990.0, 10_000.0):
        for sub in range(N_SUBBANDS):
            replay2._ingest_bp(
                ctx, FakeRecord(raw_headers(sub, int(ts), nsig=2), body.tobytes(), offset=90)
            )
    replay2._flush_sub(ctx)
    assert len(ShardReader(ctx.store).list("kafka_bp_sub")) == before["kafka_bp_sub"]
    ctx.store.close()


def test_a_failed_flush_keeps_the_buffer_then_drops_it_with_an_event(settings, monkeypatch):
    ctx = context(settings)
    collector = KafkaBandpassCollector(settings)
    body = np.ones((2, SUBBAND_NCHAN), dtype=np.float32)
    for sub in range(N_SUBBANDS):
        collector._ingest_bp(ctx, FakeRecord(raw_headers(sub, 11_000, nsig=2), body.tobytes()))
    assert len(collector._sub_buf) == 1

    def boom(*_a, **_k):
        raise OSError("store went away")

    monkeypatch.setattr(ctx.shards, "write", boom)
    for attempt in range(1, 3):
        collector._flush_sub(ctx)
        assert len(collector._sub_buf) == 1, f"buffer lost on attempt {attempt}"
        assert ctx.store.events(kind="kafka_bp_flush_failed") == []
    collector._flush_sub(ctx)  # third failure
    assert collector._sub_buf == []
    events = ctx.store.events(kind="kafka_bp_flush_failed")
    assert len(events) == 1 and events[0]["severity"] == "error"
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
    rows = [rowmap.formula_row(p) for p in inputs]
    # Inputs 0 and 1 are swapped on the wire: row 0's measured data really is
    # input 1's shape and vice versa, and both correlate strongly (1.0), so
    # this is real evidence of a disagreement rather than a weak score.
    mapping = rowmap.validate_rows(rows, truth[[1, 0, 2, 3]], inputs, truth, freq)
    assert mapping[rows[0]]["status"] == "mismatch"
    assert mapping[rows[0]]["packet_idx"] == 0  # the formula's answer, unchanged
    assert mapping[rows[0]]["runner_up"] == pytest.approx(1.0, abs=1e-3)
    assert mapping[rows[2]]["status"] == "formula+verified"


def test_validate_rows_leaves_a_degenerate_spectrum_unverified():
    """A flat/dead spectrum (or a correlation below the floor) is no evidence:
    the row stays "formula", never "mismatch" -- a dead feed must not raise a
    red flag about the mapping."""
    freq = rowmap.freq_axis_mhz()
    truth = synthetic_bandpasses(4)
    flat = np.ones((1, NCHAN))
    mapping = rowmap.validate_rows([0], flat, [0, 1, 2, 3], truth, freq)
    assert mapping[0]["status"] == "formula"
    assert mapping[0]["runner_up"] is None
    assert "degenerate" in mapping[0]["unverified_reason"]

    nan_row = np.full((1, NCHAN), np.nan)
    nan_mapping = rowmap.validate_rows([0], nan_row, [0, 1, 2, 3], truth, freq)
    assert nan_mapping[0]["status"] == "formula"


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
    import os

    old = time.time() - 3600.0
    for idx, size in ((0, 100), (1, 100), (2, 40)):
        path = tmp_path / f"2026-09-04-16:43:47.dat.{idx}"
        path.write_bytes(b"\x00" * size)
        os.utime(path, (old, old))
    other = tmp_path / "2026-09-01-00:00:00.dat.0"
    other.write_bytes(b"\x00" * 100)
    os.utime(other, (old, old))
    obs, index = rowmap.newest_complete_observation(tmp_path, full_size=100)
    assert obs == "2026-09-04-16:43:47"
    assert index == 1


def test_newest_complete_observation_requires_the_full_size_and_a_still_file(tmp_path):
    """The old rule ("largest file of the observation") accepts a lone growing
    file; the size must be the absolute full-file size and the file must have
    been untouched for five minutes."""
    import os

    growing = tmp_path / "2026-09-04-16:43:47.dat.0"
    growing.write_bytes(b"\x00" * 40)  # only file, still growing
    with pytest.raises(RuntimeError):
        rowmap.newest_complete_observation(tmp_path, full_size=100)

    just_closed = tmp_path / "2026-09-04-16:43:47.dat.1"
    just_closed.write_bytes(b"\x00" * 100)  # full size, but mtime is now
    with pytest.raises(RuntimeError):
        rowmap.newest_complete_observation(tmp_path, full_size=100)

    old = time.time() - 3600.0
    os.utime(just_closed, (old, old))
    assert rowmap.newest_complete_observation(tmp_path, full_size=100) == (
        "2026-09-04-16:43:47",
        1,
    )


def test_purge_row_map_drops_rows_no_longer_wired(tmp_path, store):
    csv_path = tmp_path / "layout.csv"
    csv_path.write_text("antenna,snap,adc,packet_idx,functional\n1,0,0,0,1\n3,0,2,2,1\n")
    rowmap.save_row_map(
        store,
        {
            0: {"packet_idx": 0, "corr": 0.9, "runner_up": 0.1, "status": "formula+verified"},
            4: {"packet_idx": 2, "corr": 0.9, "runner_up": 0.1, "status": "formula+verified"},
            8: {"packet_idx": 4, "corr": 0.9, "runner_up": 0.1, "status": "mismatch"},
        },
        {"obs": "x"},
    )
    assert rowmap.purge_row_map(store, csv_path) == [8]
    assert sorted(rowmap.load_row_map(store)) == [0, 4]
    assert 8 not in rowmap.current_mapping(store, layout_path=csv_path)


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
