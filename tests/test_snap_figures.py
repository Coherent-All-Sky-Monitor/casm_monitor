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
    "9,0,8,8,1,0,N21,E1,192.168.120.52,A\n"
    "13,1,0,12,1,1,N11,E1,192.168.120.51,I\n"
    "15,1,2,14,1,0,N11,E3,192.168.120.51,I\n"
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


def test_dark_theme_preserves_scientific_data_and_axes():
    from matplotlib.figure import Figure
    from matplotlib.colors import to_rgba
    fig = Figure()
    ax = fig.subplots()
    line, = ax.plot([500, 375], [10, 20], color=sf.SIGNAL)
    ax.set_xlabel('Frequency (MHz)')
    ax.set_ylabel('Power (dB)')
    ax.set_xlim(500, 375)
    before = line.get_data()
    sf.dark_scientific_style(fig)
    assert fig.get_facecolor() == to_rgba('#000000')
    assert ax.get_facecolor() == to_rgba('#000000')
    assert line.get_color() == '#77c7cf'
    assert ax.get_xlim() == (500, 375)
    assert ax.get_xlabel() == 'Frequency (MHz)'
    assert ax.get_ylabel() == 'Power (dB)'
    np.testing.assert_array_equal(line.get_xdata(), before[0])
    np.testing.assert_array_equal(line.get_ydata(), before[1])


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
    # ``A<n>`` is the layout's own 0-indexed ``adc`` column (== packet_idx %
    # 12), never ``adc + 1`` -- see the 2026-09-08 fix in ``_panel_title``.
    assert "ant 1" in title and "pkt 0" in title and "S0 A0" in title
    unwired = next(i for i in board["inputs"] if i["adc"] == 5)
    title2, color2 = sf._panel_title(board, unwired)
    assert title2.endswith("unwired")
    assert color2 == sf.MUTED


def test_panel_title_adc_label_is_not_off_by_one(settings, layouts):
    """2026-09-08 regression: a stray ``+ 1`` mislabelled every ADC panel
    (packet_idx 8 -- feng 0, adc 8, ant 9 -- read "S0 A9" instead of "S0 A8";
    packet_idx 14 -- feng 1, adc 2, ant 15 -- read "S1 A3" instead of "S1 A2")."""
    boards = sf.board_table()
    board0 = next(b for b in boards if b["ip"] == "192.168.120.52")
    item8 = next(i for i in board0["inputs"] if i["packet_idx"] == 8)
    title, _color = sf._panel_title(board0, item8)
    assert title == "ant 9  N21E1  (S0 A8, pkt 8)"

    board1 = next(b for b in boards if b["ip"] == "192.168.120.51")
    item14 = next(i for i in board1["inputs"] if i["packet_idx"] == 14)
    title14, _color = sf._panel_title(board1, item14)
    assert "S1 A2" in title14
    assert "pkt 14" in title14


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


def test_finish_board_blocks_autoscales_per_panel(settings, layouts):
    """2026-09-08 fix: a dead/railed input's flat, far-off spectrum used to
    widen the whole board's *shared* y-range, squashing every healthy panel
    on that board to a flat line (see ``spectra_correlator@1x.png``, ant 12
    at -60 dB and ant 33's ripple down to 10 dB). Each panel must now get its
    own robust range instead."""
    fig = sf.Figure()
    sf.FigureCanvasAgg(fig)
    ax_healthy = fig.add_subplot(1, 2, 1)
    ax_dead = fig.add_subplot(1, 2, 2)
    freq = np.linspace(400.0, 500.0, 64)
    healthy = 2.0 * np.sin(freq / 5.0)  # a few dB of real bandpass shape
    dead = np.full_like(freq, -60.0)  # railed input, way off the healthy range
    ax_healthy.plot(freq, healthy, color=sf.SIGNAL)
    ax_dead.plot(freq, dead, color=sf.SIGNAL)
    block = sf._BoardBlock(
        ip="1.2.3.4", nrows=1, ncols=2,
        cells=[(ax_healthy, 0, 0), (ax_dead, 0, 1)],
    )
    sf._finish_board_blocks([block])

    lo_h, hi_h = ax_healthy.get_ylim()
    lo_d, hi_d = ax_dead.get_ylim()
    # The dead panel's range must not leak into the healthy one: the healthy
    # panel's range should stay near its own data, not stretch down to -60 dB.
    assert lo_h > -20.0
    assert hi_h - lo_h >= sf.PANEL_YLIM_MIN_SPAN_DB
    # The dead panel gets its own (flat-input) range, at least the minimum span.
    assert lo_d < -50.0
    assert hi_d - lo_d >= sf.PANEL_YLIM_MIN_SPAN_DB
    # y tick labels stay on every panel now (values genuinely differ).
    assert all(t.get_visible() for t in ax_healthy.yaxis.get_ticklabels())
    assert all(t.get_visible() for t in ax_dead.yaxis.get_ticklabels())


def test_render_kind_bad_set_or_kind(settings, layouts):
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        with pytest.raises(ValueError):
            sf.render_kind(store, settings, "trend", "nonsense")
        with pytest.raises(ValueError):
            sf.render_kind(store, settings, "not_a_kind", "beamforming")
    finally:
        store.close()
