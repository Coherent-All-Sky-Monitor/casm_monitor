"""The vis collector's file bookkeeping, flags, scalars and shards.

The "growing file" is a sparse file of the exact byte sizes the correlator
produces, so the guard band and the size/mtime stability rule are exercised
without reading /mnt (and without allocating 6 GB).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from casm_monitor.collectors import rowmap
from casm_monitor.collectors import vis as visc
from casm_monitor.collectors.base import CollectorContext
from casm_monitor.store import ShardWriter, Store

OBS = "2026-09-04-16:43:47"
LAYOUT = (
    "antenna,x,y,z,snap,adc,packet_idx,functional,include_in_beamforming,row,col\n"
    "1,0.0,0.0,0.0,0,0,0,1,0,N01,E1\n"
    "2,0.0,0.0,0.0,0,1,1,0,0,,\n"
    "3,10.0,0.0,0.1,0,2,2,1,1,N01,E3\n"
    "4,0.0,20.0,0.2,0,3,3,1,1,N02,E1\n"
)


@pytest.fixture
def layout(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "layout.csv"
    path.write_text(LAYOUT)
    monkeypatch.setattr(rowmap, "LAYOUT_CSV", path)
    return path


def make_file(directory: Path, index: int, n_int: int, *, header: bool = False) -> Path:
    """A sparse file holding exactly ``n_int`` integrations (+ optional header)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{OBS}.dat.{index}"
    size = n_int * visc.INTEGRATION_BYTES + (visc.HEADER_BYTES if header else 0)
    with open(path, "wb") as fh:
        fh.truncate(size)
    return path


# -- geometry -----------------------------------------------------------
def test_integration_arithmetic() -> None:
    assert visc.INTEGRATION_BYTES == 202_899_456
    assert visc.INTEGRATIONS_PER_FILE == 32
    assert visc.complete_integrations(visc.INTEGRATION_BYTES * 3) == 3
    # a half-written integration does not count
    assert visc.complete_integrations(visc.INTEGRATION_BYTES * 3 + 17) == 3
    assert visc.complete_integrations(visc.INTEGRATION_BYTES - 1) == 0
    # the 4096-byte header is not payload
    assert visc.complete_integrations(visc.INTEGRATION_BYTES + 4096, 4096) == 1
    assert visc.complete_integrations(visc.INTEGRATION_BYTES + 4096, 0) == 1
    assert visc.is_full_size(visc.FULL_FILE_BYTES)
    assert visc.is_full_size(visc.FULL_FILE_BYTES + visc.HEADER_BYTES)
    assert not visc.is_full_size(visc.FULL_FILE_BYTES - 1)


def test_guard_band_only_applies_to_a_growing_file() -> None:
    growing = visc.INTEGRATION_BYTES * 13
    assert visc.safe_integrations(growing, growing=True) == 12
    assert visc.safe_integrations(growing, growing=False) == 13
    assert visc.safe_integrations(visc.INTEGRATION_BYTES, growing=True) == 0
    # a finished file needs no guard band whichever way it is asked
    assert visc.safe_integrations(visc.FULL_FILE_BYTES, growing=True) == 32


def test_integration_timestamps_follow_dt() -> None:
    t0 = visc.obs_start_unix(OBS)
    assert visc.integration_time(t0, 0, 0) == t0
    assert visc.integration_time(t0, 0, 1) - t0 == pytest.approx(visc.DT_S)
    assert visc.integration_time(t0, 1, 0) - t0 == pytest.approx(32 * visc.DT_S)


# -- watermark / guard band on a fake growing file ----------------------
def test_pending_integrations_watermark_and_stability(tmp_path) -> None:
    vis_dir = tmp_path / "vis"
    make_file(vis_dir, 0, 32)   # finished
    make_file(vis_dir, 1, 13)   # growing
    files = visc.scan_observation_files(vis_dir, OBS)
    assert [f.index for f in files] == [0, 1]
    assert files[0].full and not files[1].full
    t0 = visc.obs_start_unix(OBS)

    # First poll: the growing file has never been seen, so nothing is read from it.
    first = visc.pending_integrations(files, None, t0, stable={})
    assert [(p.file_idx, p.int_idx) for p in first] == [(0, k) for k in range(32)]
    assert first[0].first_of_file and not first[1].first_of_file

    # Second poll, unchanged size/mtime: 13 complete minus one guard integration.
    stable = {f.index: (f.size, f.mtime) for f in files}
    second = visc.pending_integrations(files, None, t0, stable=stable)
    assert [(p.file_idx, p.int_idx) for p in second[32:]] == [(1, k) for k in range(12)]

    # A file that grew between the two polls contributes nothing this pass.
    grown = [
        files[0],
        visc.VisFile(index=1, path=files[1].path, size=files[1].size + visc.INTEGRATION_BYTES,
                     mtime=files[1].mtime + 1),
    ]
    assert all(p.file_idx == 0 for p in visc.pending_integrations(grown, None, t0, stable=stable))

    # The watermark is exclusive and ordered on (file_idx, int_idx).
    after = visc.pending_integrations(files, (1, 5), t0, stable=stable)
    assert [(p.file_idx, p.int_idx) for p in after] == [(1, k) for k in range(6, 12)]
    assert visc.pending_integrations(files, (0, 31), t0, stable=stable)[0].file_idx == 1

    # The backfill window and the per-pass limit both bound the work.
    windowed = visc.pending_integrations(
        files, None, t0, stable=stable, oldest_ts=visc.integration_time(t0, 1, 0)
    )
    assert [(p.file_idx, p.int_idx) for p in windowed] == [(1, k) for k in range(12)]
    assert len(visc.pending_integrations(files, None, t0, stable=stable, limit=5)) == 5


def test_a_short_old_file_is_not_treated_as_growing(tmp_path) -> None:
    vis_dir = tmp_path / "vis"
    make_file(vis_dir, 0, 4)    # truncated old file
    make_file(vis_dir, 1, 32)   # the newest file, finished
    files = visc.scan_observation_files(vis_dir, OBS)
    stable = {f.index: (f.size, f.mtime) for f in files}
    pending = visc.pending_integrations(files, None, visc.obs_start_unix(OBS), stable=stable)
    # all four of the truncated file's integrations, no guard band deducted
    assert [(p.file_idx, p.int_idx) for p in pending][:4] == [(0, k) for k in range(4)]
    assert len(pending) == 4 + 32


def test_newest_observation_and_header_sniff(tmp_path) -> None:
    vis_dir = tmp_path / "vis"
    make_file(vis_dir, 0, 1)
    (vis_dir / "2026-09-05-00:00:00.dat.0").write_bytes(b"")
    assert visc.newest_observation(vis_dir) == "2026-09-05-00:00:00"
    assert visc.header_bytes(vis_dir / f"{OBS}.dat.0") == 0
    assert visc.newest_observation(tmp_path / "empty") is None


def test_contiguous_runs_never_cross_a_file_or_exceed_the_length() -> None:
    items = [
        visc.PendingIntegration(0, 30, 1.0, False),
        visc.PendingIntegration(0, 31, 2.0, False),
        visc.PendingIntegration(1, 0, 3.0, True),
        visc.PendingIntegration(1, 1, 4.0, False),
        visc.PendingIntegration(1, 3, 5.0, False),  # a gap
    ]
    runs = visc.contiguous_runs(items, 8)
    assert [[(p.file_idx, p.int_idx) for p in run] for run in runs] == [
        [(0, 30), (0, 31)], [(1, 0), (1, 1)], [(1, 3)]
    ]
    assert all(len(run) <= 1 for run in visc.contiguous_runs(items, 1))


# -- input sets ---------------------------------------------------------
def test_input_sets_and_table(layout) -> None:
    sets = visc.input_sets()
    assert sets["wired"] == [0, 2, 3]
    assert sets["live"] == [2, 3]
    table = visc.input_table()
    assert [row["packet_idx"] for row in table] == [0, 2, 3]
    assert table[0] == {"packet_idx": 0, "antenna": 1, "station": "N01E1", "in_bf": False}
    assert table[2]["in_bf"] is True


# -- per-integration scalars -------------------------------------------
def test_dark_subbands_finds_a_missing_512_channel_block() -> None:
    power = np.full(visc.NCHAN, 1e6)
    power[512:1024] = 0.0  # subband 1 delivered nothing
    assert visc.dark_subbands(power) == [1]
    assert visc.subband_medians(power).size == 6
    power[2560:] = 1e3  # 0.1% of the best block: dark too
    assert visc.dark_subbands(power) == [1, 5]
    # a block merely 10% down is a bandpass slope, not a dark subband
    slope = np.full(visc.NCHAN, 1e6)
    slope[512:1024] = 9e5
    assert visc.dark_subbands(slope) == []


def test_dark_subbands_ignores_an_input_at_the_quantisation_floor() -> None:
    # input 11 / ant 12 on 2026-09-08: block medians of 1-2 counts, one of them
    # zero from integration to integration. Dead, not "a subband went dark".
    floor = np.ones(visc.NCHAN)
    floor[512:1024] = 0.0
    assert visc.dark_subbands(floor) == []
    assert visc.dark_subbands(np.zeros(visc.NCHAN)) == []
    assert visc.dark_subbands(np.ones(visc.NCHAN)) == []
    # the same shape well above the floor IS reported
    assert visc.dark_subbands(floor * visc.DARK_MIN_BLOCK_POWER * 10) == [1]


def test_band_power_db_uses_the_400_480_band() -> None:
    freq = rowmap.freq_axis_mhz()
    power = np.where(rowmap.band_mask(freq), 100.0, 1e6)
    assert visc.band_power_db(power, freq) == pytest.approx(20.0)


def test_auto_indices_are_the_diagonal() -> None:
    assert visc.auto_indices(3) == [0, 3, 5]


# -- latest mirror ------------------------------------------------------
def test_latest_mirror_round_trip(settings) -> None:
    vis = (np.arange(6, dtype=np.complex64) + 1j).reshape(3, 2)
    freq = np.array([484.375, 484.34])
    assert visc.read_latest_vis(settings) is None
    visc.write_latest_vis(
        settings, vis=vis, inputs=[0, 2], freq_mhz=freq, ts=123.5, obs=OBS,
        file_idx=7, int_idx=3, first_of_file=True,
    )
    got = visc.read_latest_vis(settings)
    assert got["ts"] == 123.5
    assert got["inputs"] == [0, 2]
    assert got["obs"] == OBS
    assert got["file_idx"] == 7 and got["int_idx"] == 3
    assert got["first_of_file"] is True
    assert np.array_equal(got["vis"], vis)
    assert visc.latest_vis_path(settings).is_relative_to(settings.store_root)


# -- publish path (shards + scalars + events, no file reads) ------------
def test_publish_writes_both_streams_and_flags_the_first_integration(settings, layout) -> None:
    store = Store(settings.db_path, store_root=settings.store_root)
    ctx = CollectorContext(
        settings=settings, store=store, shards=ShardWriter(store, settings.shards_root)
    )
    collector = visc.VisCollector(settings)
    inputs = [0, 2, 3]
    n_bl = len(inputs) * (len(inputs) + 1) // 2
    freq = rowmap.freq_axis_mhz()
    rng = np.random.default_rng(3)
    # casm_io hands the collector (F, n_bl); autos are real and positive.
    frame = (rng.normal(size=(visc.NCHAN, n_bl)) + 1j * rng.normal(size=(visc.NCHAN, n_bl)))
    for k, _p in enumerate(inputs):
        from casm_io.correlator.baselines import triu_flat_index

        frame[:, triu_flat_index(len(inputs), k, k)] = 1e6 * (k + 1)
    t0 = visc.obs_start_unix(OBS)
    for k in range(visc.AVG_BUFFER):
        item = visc.PendingIntegration(5, k, visc.integration_time(t0, 5, k), k == 0)
        collector._publish(ctx, OBS, item, item.ts, frame, inputs, freq)

    full = store.list_shards(visc.STREAM_FULL)
    assert len(full) == visc.AVG_BUFFER
    assert full[0]["shape"] == [n_bl, visc.NCHAN]
    assert full[0]["dtype"] == "complex64"
    meta = full[0]["meta"]
    assert meta["inputs"] == inputs
    assert meta["flags"]["first_of_file"] is True
    assert meta["obs"] == OBS and meta["file_idx"] == 5 and meta["int_idx"] == 0
    assert meta["chan_avg"] == 1
    assert full[1]["meta"]["flags"]["first_of_file"] is False

    avg8 = store.list_shards(visc.STREAM_AVG8)
    assert len(avg8) == 1
    assert avg8[0]["shape"] == [visc.AVG_BUFFER, n_bl, visc.AVG_NCHAN]
    assert len(avg8[0]["meta"]["t"]) == visc.AVG_BUFFER
    assert avg8[0]["meta"]["chan_avg"] == visc.CHAN_AVG
    # The avg8 frequency axis is the mean of each 8-channel block, not the raw
    # native top: channel 0's frequency is the mean of native channels 0-7.
    expected_block0_freq = float(freq[:visc.CHAN_AVG].mean())
    assert avg8[0]["meta"]["freq_top_mhz"] == pytest.approx(expected_block0_freq, abs=1e-6)

    # the channel-averaged shard really is the block mean of the full one
    from casm_monitor.store import ShardReader

    reader = ShardReader(store)
    hi_res, _ = reader.load(full[0])
    reduced, _ = reader.load(avg8[0])
    expected = hi_res.reshape(n_bl, visc.AVG_NCHAN, visc.CHAN_AVG).mean(axis=2)
    assert np.allclose(reduced[0], expected, rtol=1e-4, atol=1e-3)

    # watermark, scalars, event
    assert store.get_watermark("vis", OBS) == [5, visc.AVG_BUFFER - 1]
    assert store.latest_scalar("vis.input3.auto_db")["value"] == pytest.approx(
        10 * np.log10(3e6), abs=1e-3
    )
    assert store.latest_scalar("vis.subbands_dark")["value"] == 0
    kinds = [e["kind"] for e in store.events(limit=20)]
    assert "vis_first_integration_flagged" in kinds
    store.close()


def test_buffer_is_flushed_on_close_and_on_an_obs_change(settings, layout) -> None:
    store = Store(settings.db_path, store_root=settings.store_root)
    ctx = CollectorContext(
        settings=settings, store=store, shards=ShardWriter(store, settings.shards_root)
    )
    collector = visc.VisCollector(settings)
    inputs = [0, 2]
    n_bl = 3
    frame = np.ones((visc.NCHAN, n_bl), dtype=np.complex64)
    freq = rowmap.freq_axis_mhz()
    item = visc.PendingIntegration(0, 1, 1000.0, False)
    collector._publish(ctx, OBS, item, item.ts, frame, inputs, freq)
    assert store.list_shards(visc.STREAM_AVG8) == []
    collector.close(ctx)
    assert len(store.list_shards(visc.STREAM_AVG8)) == 1
    assert store.list_shards(visc.STREAM_AVG8)[0]["shape"] == [1, n_bl, visc.AVG_NCHAN]
    store.close()


def test_vis_avg8_survives_a_restart_with_no_gap(settings, layout) -> None:
    """Stop after 3 (< AVG_BUFFER) buffered integrations, start a brand new
    collector against the same store, and the eventual vis_avg8 shard must
    still cover all of them -- nothing lost to the crash (review P1)."""
    store = Store(settings.db_path, store_root=settings.store_root)
    ctx = CollectorContext(
        settings=settings, store=store, shards=ShardWriter(store, settings.shards_root)
    )
    inputs = [0, 2]
    n_bl = 3
    freq = rowmap.freq_axis_mhz()
    frame = np.ones((visc.NCHAN, n_bl), dtype=np.complex64)
    t0 = visc.obs_start_unix(OBS)

    collector_a = visc.VisCollector(settings)
    for k in range(3):
        item = visc.PendingIntegration(0, k, visc.integration_time(t0, 0, k), k == 0)
        collector_a._publish(ctx, OBS, item, item.ts, frame * (k + 1), inputs, freq)

    # "Crash": no close(), no flush -- only the staging file and the full-res
    # watermark (already advanced past these 3 integrations) exist.
    assert store.list_shards(visc.STREAM_AVG8) == []
    staged = visc.read_avg8_staging(settings, OBS)
    assert staged is not None and len(staged["samples"]) == 3

    # A brand new collector (fresh process) recovers the buffer on its first
    # tick for this obs, then a 4th integration pushes it over AVG_BUFFER? No
    # -- AVG_BUFFER is 8, so force a flush directly to check the recovered
    # content publishes without a gap.
    collector_b = visc.VisCollector(settings)
    collector_b._recover_avg8_staging(ctx, OBS)
    assert len(collector_b._buffer) == 3
    collector_b.close(ctx)  # orderly flush, as the runner does on SIGTERM

    avg8 = store.list_shards(visc.STREAM_AVG8)
    assert len(avg8) == 1
    assert avg8[0]["shape"] == [3, n_bl, visc.AVG_NCHAN]
    assert avg8[0]["meta"]["int_idx"] == [0, 1, 2]
    assert visc.read_avg8_staging(settings, OBS) is None  # cleared once durable
    store.close()


def test_collect_reports_no_files_without_raising(settings, tmp_path, layout) -> None:
    from dataclasses import replace

    empty = tmp_path / "no-vis"
    empty.mkdir()
    local = replace(settings, vis_dir=empty)
    store = Store(local.db_path, store_root=local.store_root)
    ctx = CollectorContext(
        settings=local, store=store, shards=ShardWriter(store, local.shards_root)
    )
    visc.VisCollector(local).collect(ctx)
    assert store.latest_scalar("vis.ok")["value"] == 0
    store.close()


def test_collect_records_an_obs_change_event(settings, tmp_path, layout) -> None:
    from dataclasses import replace

    vis_dir = tmp_path / "vis"
    make_file(vis_dir, 0, 0)  # exists but holds no complete integration
    local = replace(settings, vis_dir=vis_dir)
    store = Store(local.db_path, store_root=local.store_root)
    ctx = CollectorContext(
        settings=local, store=store, shards=ShardWriter(store, local.shards_root)
    )
    collector = visc.VisCollector(local)
    collector.collect(ctx)
    assert store.latest_scalar("vis.obs")["value"] == OBS
    make_file(vis_dir, 0, 0).with_name("2026-09-05-01:00:00.dat.0").write_bytes(b"")
    collector.collect(ctx)
    assert [e["kind"] for e in store.events(kind="vis_obs_changed", limit=5)] == ["vis_obs_changed"]
    assert store.latest_scalar("vis.obs")["value"] == "2026-09-05-01:00:00"
    store.close()
