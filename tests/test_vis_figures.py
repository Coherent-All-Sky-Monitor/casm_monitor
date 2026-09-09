"""Smoke tests for casm_monitor.figures.vis_figures on synthetic vis_avg8 shards.

No /mnt read: the shards, the layout and the latest-integration mirror are all
synthetic and the timestamps are anchored on ``time.time()`` so they always
fall inside the figures' fixed 24 h window regardless of when the suite runs.
"""

from __future__ import annotations

import struct
import time

import numpy as np
import pytest

from casm_monitor.collectors import rowmap
from casm_monitor.collectors import vis as visc
from casm_monitor.figures import vis_figures as vf
from casm_monitor.store import ShardWriter, Store
from casm_monitor.web import vis as webvis

OBS = "2026-09-04-16:43:47"
LAYOUT = (
    "antenna,x,y,z,snap,adc,packet_idx,functional,include_in_beamforming,row,col\n"
    "1,0.0,0.0,0.0,0,0,0,1,0,N01,E1\n"
    "2,0.0,0.0,0.0,0,1,1,0,0,,\n"
    "3,120.0,0.0,0.1,0,2,2,1,1,N01,E3\n"
    "4,0.0,90.0,0.2,0,3,3,1,1,N02,E1\n"
)
INPUTS = [0, 2, 3]
N_BL = 6
N_SAMPLES = 20
DT_S = 600.0  # 10 min cadence, well inside the 24 h window


@pytest.fixture
def layout(tmp_path, monkeypatch):
    path = tmp_path / "layout.csv"
    path.write_text(LAYOUT)
    monkeypatch.setattr(rowmap, "LAYOUT_CSV", path)
    webvis._positions_cache.clear()
    return path


def _frame(scale: float, nchan: int) -> np.ndarray:
    from casm_io.correlator.baselines import triu_flat_index

    freq = np.linspace(rowmap.FREQ_TOP_MHZ, rowmap.FREQ_TOP_MHZ - 100.0, nchan)
    arr = np.zeros((N_BL, nchan), dtype=np.complex64)
    for k in range(len(INPUTS)):
        arr[triu_flat_index(len(INPUTS), k, k)] = 1e6 * (k + 1) * scale
    for a in range(len(INPUTS)):
        for b in range(a + 1, len(INPUTS)):
            idx = triu_flat_index(len(INPUTS), a, b)
            arr[idx] = 1e5 * scale * np.exp(2j * np.pi * freq / 7.0 * (b - a))
    return arr


@pytest.fixture
def seeded(settings, layout):
    """One vis_avg8 shard, N_SAMPLES integrations, anchored on ``now``."""
    store = Store(settings.db_path, store_root=settings.store_root)
    shards = ShardWriter(store, settings.shards_root)
    now = time.time()
    times = [now - (N_SAMPLES - 1 - k) * DT_S for k in range(N_SAMPLES)]
    nchan = visc.AVG_NCHAN
    arr = np.stack([_frame(1.0 + 0.01 * k, nchan) for k in range(N_SAMPLES)]).astype(np.complex64)
    shards.write(
        visc.STREAM_AVG8,
        arr,
        t0=times[0],
        t1=times[-1],
        meta={
            "obs": OBS,
            "inputs": INPUTS,
            "t": times,
            "chan_avg": visc.CHAN_AVG,
            "freq_top_mhz": rowmap.FREQ_TOP_MHZ - 0.5 * (visc.CHAN_AVG - 1) * rowmap.CHAN_BW_MHZ,
            "chan_bw_mhz": rowmap.CHAN_BW_MHZ * visc.CHAN_AVG,
            "freq_order": "descending",
            "flags": {"first_of_file": [False] * N_SAMPLES},
        },
        chunks=(1, N_BL, nchan),
    )
    visc.write_latest_vis(
        settings,
        vis=_frame(1.1, visc.NCHAN),
        inputs=INPUTS,
        freq_mhz=rowmap.freq_axis_mhz(),
        ts=times[-1],
        obs=OBS,
        file_idx=0,
        int_idx=N_SAMPLES - 1,
        first_of_file=False,
    )
    yield times
    store.close()


def _png_size(data: bytes) -> tuple[int, int]:
    """Width/height from a PNG's IHDR chunk, no Pillow dependency."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_load_window(settings, seeded):
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        window = vf.load_window(store, settings, "wired")
        assert window.stream == visc.STREAM_AVG8
        assert len(window.times) == N_SAMPLES
        assert window.inputs == INPUTS
        assert window.z.shape[0] == N_SAMPLES
    finally:
        store.close()


def test_load_window_no_data_raises(settings, layout):
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        with pytest.raises(vf.NoData):
            vf.load_window(store, settings, "wired")
    finally:
        store.close()


@pytest.mark.parametrize("kind", list(vf.MATRIX_KINDS) + list(vf.SPECTRA_KINDS) + ["autos"])
@pytest.mark.parametrize("set_name", vf.SETS)
def test_render_kind_raw(settings, seeded, kind, set_name):
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        pngs, info = vf.render_kind(store, settings, kind, set_name, "raw")
    finally:
        store.close()
    assert set(pngs) == {"1x", "2x"}
    assert len(pngs["1x"]) > 500
    assert len(pngs["2x"]) > 500
    w1, h1 = _png_size(pngs["1x"])
    w2, h2 = _png_size(pngs["2x"])
    # Same figure, two dpi's: the 2x render is strictly bigger in pixels.
    assert w2 > w1 and h2 > h1
    assert info["n_integrations"] == N_SAMPLES
    assert info["stream"] == visc.STREAM_AVG8
    assert info["obs"] == OBS


def test_render_kind_sun_ref(settings, seeded):
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        pngs, info = vf.render_kind(store, settings, "matrix_phase", "live", "sun")
    finally:
        store.close()
    assert len(pngs["1x"]) > 500
    assert info["inputs"] == [2, 3]


def test_render_kind_bad_set_or_ref(settings, seeded):
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        with pytest.raises(ValueError):
            vf.render_kind(store, settings, "autos", "everything", "raw")
        with pytest.raises(ValueError):
            vf.render_kind(store, settings, "autos", "wired", "nonsense")
        with pytest.raises(ValueError):
            vf.render_kind(store, settings, "not_a_kind", "wired", "raw")
    finally:
        store.close()


def test_matrix_dimensions_scale_with_inputs(settings, seeded):
    """1.6 in panels at dpi 110: a 3-input matrix is roughly 3*1.6*110 px wide."""
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        pngs, _info = vf.render_kind(store, settings, "matrix_amp", "wired", "raw")
    finally:
        store.close()
    w2x, _h2x = _png_size(pngs["2x"])
    expected = len(INPUTS) * vf.PANEL_IN * vf.DPI_2X
    assert abs(w2x - expected) < expected * 0.15
