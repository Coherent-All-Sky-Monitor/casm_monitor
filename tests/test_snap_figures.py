"""Smoke tests for casm_monitor.figures.snap_figures on synthetic frames/reads.

No zapdos, no /mnt read: the layout/snap-map CSVs, the Kafka bandpass shards,
the latest-frame mirror and the board read are all synthetic and anchored on
``time.time()`` so they always fall inside the figures' fixed 24 h window.
"""

from __future__ import annotations

import json
import struct
import time

import numpy as np
import pytest

from casm_monitor.collectors import rowmap
from casm_monitor.collectors.kafka_bp import NCHAN, SUB_NCHAN, latest_frame_path
from casm_monitor.figures import snap_figures as sf
from casm_monitor.jobs.snap_read import ensure_latest_table
from casm_monitor.store import ShardWriter, Store

LAYOUT = (
    "antenna,snap,adc,packet_idx,functional,include_in_beamforming,row,col,snap_ip,slot\n"
    "1,0,0,0,1,1,N21,E1,192.168.120.52,A\n"
    "2,0,1,1,0,0,,,,\n"
    "3,0,2,2,1,0,N21,E4,192.168.120.52,A\n"
    "13,1,0,12,1,1,N11,E1,192.168.120.51,I\n"
)
SNAP_MAP = "chassis,slot,feng_id,snap_ip\n1,A,0,192.168.120.52\n1,I,1,192.168.120.51\n"

# Formula rows (row = 2 * packet_idx) for the three wired inputs above.
ROW_0, ROW_2, ROW_24 = 0, 4, 24


@pytest.fixture
def layouts(tmp_path, monkeypatch):
    layout = tmp_path / "layout.csv"
    snap_map = tmp_path / "snap_map.csv"
    layout.write_text(LAYOUT)
    snap_map.write_text(SNAP_MAP)
    monkeypatch.setattr(rowmap, "LAYOUT_CSV", layout)
    monkeypatch.setattr(rowmap, "SNAP_MAP_CSV", snap_map)
    return layout, snap_map


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def seed_kafka_sub(store, settings, rows: list[int], now: float, n_samples: int = 30, dt_s: float = 600.0):
    shards = ShardWriter(store, settings.shards_root)
    times = [now - (n_samples - 1 - k) * dt_s for k in range(n_samples)]
    array = np.stack(
        [
            np.stack([np.linspace(-3.0 - r * 0.1 + 0.01 * k, 5.0, SUB_NCHAN) for r in rows])
            for k in range(n_samples)
        ]
    ).astype(np.float32)
    shards.write(
        "kafka_bp_sub",
        array,
        t0=times[0],
        t1=times[-1],
        meta={"rows": rows, "t": times, "units": "dB", "chan_avg": rowmap.NCHAN // SUB_NCHAN},
    )
    return times


def seed_latest_frame(settings, rows: list[int], now: float) -> None:
    path = latest_frame_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    power_db = np.stack([np.linspace(-2.0, 6.0, NCHAN, dtype=np.float32) for _ in rows])
    np.savez(
        path,
        ts=np.float64(now - 5.0),
        written=np.float64(now),
        rows=np.asarray(rows, dtype=np.int32),
        subbands_ok=np.asarray([True] * 6),
        power_db=power_db,
    )


def seed_board_read(store, settings, ip: str, ts: float, *, rms=None) -> int:
    ensure_latest_table(store)
    shards = ShardWriter(store, settings.shards_root)
    spectra = np.abs(np.random.default_rng(0).normal(1.0, 0.1, size=(12, 4096))).astype(np.float32)
    row = shards.write("snap_read", spectra, t0=ts, meta={"n_chans": 4096})
    summary = {
        "adc_rms": rms or [round(0.5 + 0.01 * i, 3) for i in range(12)],
        "n_chans": 4096,
    }
    store.execute(
        "INSERT INTO snap_read_latest (ip, ts, shard_id, summary) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(ip) DO UPDATE SET ts = excluded.ts, shard_id = excluded.shard_id, "
        "summary = excluded.summary",
        (ip, ts, row["id"], json.dumps(summary)),
    )
    return int(row["id"])


def seed_trend_scalars(store, now: float, n=30, dt_s=600.0):
    times = [now - (n - 1 - k) * dt_s for k in range(n)]
    for t in times:
        store.put_scalar("kafka_bp.row0.power_db", -3.0, tags={"row": 0, "packet_idx": 0}, ts=t)
        store.put_scalar("kafka_bp.row24.power_db", -4.0, tags={"row": 24, "packet_idx": 12}, ts=t)
    store.put_scalar(
        "snaps.night_median_db", -4.2, tags={"row": 24, "packet_idx": 12, "date": "2026-09-08"},
        ts=times[0] + 30.0,
    )
    return times


def test_boards_inputs_beamforming_filters_and_groups(settings, layouts):
    boards = sf.board_table()
    grouped = sf._boards_inputs(boards, "beamforming")
    # Both antenna boards have exactly one in-BF adc in the fixture layout.
    assert [b["ip"] for b, _ in grouped] == ["192.168.120.52", "192.168.120.51"]
    assert all(len(items) == 1 for _, items in grouped)

    grouped_all = sf._boards_inputs(boards, "all12")
    assert all(len(items) == 12 for _, items in grouped_all)


def test_panel_title_wired_and_unwired(settings, layouts):
    boards = sf.board_table()
    board = next(b for b in boards if b["ip"] == "192.168.120.52")
    wired = next(i for i in board["inputs"] if i["adc"] == 0)
    title, color = sf._panel_title(board, wired)
    assert "ant 1" in title and "pkt 0" in title and "S0 A1" in title
    unwired = next(i for i in board["inputs"] if i["adc"] == 5)
    title2, color2 = sf._panel_title(board, unwired)
    assert title2.endswith("unwired")
    assert color2 == sf.MUTED


def test_render_spectra_correlator(settings, layouts):
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        now = time.time()
        rows = [ROW_0, ROW_2, ROW_24]
        seed_kafka_sub(store, settings, rows, now)
        seed_latest_frame(settings, rows, now)
        pngs, info = sf.render_kind(store, settings, "spectra_correlator", "beamforming")
    finally:
        store.close()
    assert set(pngs) == {"1x", "2x"}
    assert len(pngs["1x"]) > 500
    w1, h1 = _png_size(pngs["1x"])
    w2, h2 = _png_size(pngs["2x"])
    assert w2 > w1 and h2 > h1
    assert info["n_frames"] == 30
    assert info["board_read_ts"] is None


def test_render_spectra_board_no_reads(settings, layouts):
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        pngs, info = sf.render_kind(store, settings, "spectra_board", "beamforming")
    finally:
        store.close()
    assert len(pngs["1x"]) > 200
    assert info["board_read_ts"] is None
    assert info["n_frames"] == 0


def test_render_spectra_board_with_reads(settings, layouts):
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        now = time.time()
        seed_board_read(store, settings, "192.168.120.52", now)
        pngs, info = sf.render_kind(store, settings, "spectra_board", "all12")
    finally:
        store.close()
    assert len(pngs["1x"]) > 500
    assert info["board_read_ts"] == pytest.approx(now)
    assert info["n_frames"] == 1


def test_render_waterfall(settings, layouts):
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        now = time.time()
        rows = [ROW_0, ROW_2, ROW_24]
        seed_kafka_sub(store, settings, rows, now, n_samples=800, dt_s=108.0)
        pngs, info = sf.render_kind(store, settings, "waterfall", "all12")
    finally:
        store.close()
    assert len(pngs["1x"]) > 500
    assert info["n_frames"] == 800


def test_render_trend(settings, layouts):
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        now = time.time()
        seed_trend_scalars(store, now)
        pngs, info = sf.render_kind(store, settings, "trend", "beamforming")
    finally:
        store.close()
    assert len(pngs["1x"]) > 500
    assert info["n_frames"] == 30


def test_render_kind_bad_set_or_kind(settings, layouts):
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        with pytest.raises(ValueError):
            sf.render_kind(store, settings, "trend", "nonsense")
        with pytest.raises(ValueError):
            sf.render_kind(store, settings, "not_a_kind", "beamforming")
    finally:
        store.close()
