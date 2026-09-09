"""Imaging (M4) renderers and the incremental frame-cache pass.

No real imaging happens here: ``allsky_snapshots`` reads /mnt and costs ~5 s
per integration, so the renderers are exercised on a synthetic snapshot dict
of exactly the shape it returns, and the job pass is driven by a fake
``image_window``.
"""

from __future__ import annotations

import json
import shutil
import time

import numpy as np
import pytest

from casm_monitor.figures import imaging_figures as imf
from casm_monitor.jobs import render_figures as rf

NPIX = 41


def synthetic_snapshot(ts: float, *, peak_lm: tuple[float, float] = (0.3, -0.2)) -> dict:
    """One ``allsky_snapshots``-shaped dict: NaN outside the horizon circle."""
    axis = np.linspace(-1.0, 1.0, NPIX)
    ll, mm = np.meshgrid(axis, axis)
    inside = ll**2 + mm**2 <= 1.0
    image = np.full((NPIX, NPIX), np.nan)
    r2 = (ll - peak_lm[0]) ** 2 + (mm - peak_lm[1]) ** 2
    image[inside] = np.exp(-r2[inside] / 0.02) + 0.05
    return {
        "time_unix": float(ts),
        "image": image,
        "l_axis": axis,
        "m_axis": axis,
        "sources": {
            "sun": (peak_lm[0], peak_lm[1], 42.0),
            "cyg-a": (-0.5, 0.4, 20.0),
            "cas-a": (0.1, 0.9, -5.0),  # below the horizon: no marker
        },
    }


class FakeConfig:
    cal_path = "/nonexistent/cal.h5"
    cal_file = "cal_sep03peak_core17.h5"
    weights_file = "/nonexistent/w.h5"
    antennas = (9, 10, 15)
    inactive_antennas = (1, 3)
    wired_antennas = (1, 3, 9, 10, 15)
    antennas_source = "deployed"


# -- renderer smoke --------------------------------------------------------
def test_render_latest_returns_both_scales():
    pngs = imf.render_latest(
        synthetic_snapshot(1788835440.0), {"cal_file": "cal_x.h5", "n_ant": 17}
    )
    assert set(pngs) == {"1x", "2x"}
    for data in pngs.values():
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(pngs["2x"]) > len(pngs["1x"])


def test_render_strip_and_frame():
    snaps = [synthetic_snapshot(1788835440.0 + 1800 * k) for k in range(3)]
    pngs = imf.render_strip(snaps)
    assert set(pngs) == {"1x", "2x"}
    assert pngs["1x"].startswith(b"\x89PNG\r\n\x1a\n")
    frame = imf.render_frame(snaps[0])
    assert frame.startswith(b"\x89PNG\r\n\x1a\n")


def test_render_strip_empty_is_a_placeholder_not_a_crash():
    pngs = imf.render_strip([])
    assert pngs["1x"].startswith(b"\x89PNG\r\n\x1a\n")


def test_source_marks_fixed_order_and_up_flag():
    marks = imf.source_marks(1788835440.0)
    assert [m["name"] for m in marks] == list(imf.SOURCES)
    for m in marks:
        assert m["up"] == (m["alt_deg"] > 0)
        assert -90.0 <= m["alt_deg"] <= 90.0
        assert 0.0 <= m["az_deg"] <= 360.0


def test_utc_iso_and_parse_utc_round_trip():
    ts = 1788835440.0
    assert imf.utc_iso(ts).endswith("Z")
    assert imf.parse_utc(imf.utc_iso(ts)) == ts
    assert imf.parse_utc("2026-09-09T02:44:00Z") == imf.parse_utc("2026-09-09T02:44:00")


def test_imaging_config_refuses_a_missing_cal(settings, monkeypatch):
    monkeypatch.setattr(
        "casm_monitor.cal_defaults.deployed_product",
        lambda s: {"cal_file": "/nonexistent/cal.h5", "weights_file": None},
    )
    with pytest.raises(imf.ImagingUnavailable):
        imf.imaging_config(settings)


def test_imaging_config_derives_inactive_from_the_layout(settings, monkeypatch, tmp_path):
    cal = tmp_path / "cal_test.h5"
    cal.write_bytes(b"not really hdf5, only its existence is checked here")
    monkeypatch.setattr(
        "casm_monitor.cal_defaults.deployed_product",
        lambda s: {"cal_file": str(cal), "weights_file": "/nonexistent/w.h5"},
    )
    monkeypatch.setattr(
        "casm_monitor.cal_defaults.deployed_cb_antennas",
        lambda p: {"antennas": [9, 15, 22], "error": None},
    )
    monkeypatch.setattr(
        "casm_monitor.cal_defaults.layout_info",
        lambda p: {"wired_antennas": [1, 9, 15, 22, 33], "antennas": [9, 15, 22]},
    )
    cfg = imf.imaging_config(settings)
    assert cfg.cal_file == "cal_test.h5"
    assert cfg.antennas == (9, 15, 22)
    # Never hardcoded: wired minus the deployed CB set.
    assert cfg.inactive_antennas == (1, 33)


# -- the job's incremental pass --------------------------------------------
@pytest.fixture
def fake_imaging(monkeypatch):
    """``imaging_config``/``image_window``/``render_movie`` replaced by fakes."""
    calls: list[tuple[float, float]] = []

    def fake_window(store, settings, t0, t1, *, config=None, **kw):
        calls.append((t0, t1))
        # Two integrations at the start of whatever range was asked for.
        return [
            synthetic_snapshot(t0 + k * imf.INTEGRATION_S)
            for k in range(2)
            if t0 + k * imf.INTEGRATION_S < t1
        ]

    monkeypatch.setattr(imf, "imaging_config", lambda settings: FakeConfig())
    monkeypatch.setattr(imf, "image_window", fake_window)
    monkeypatch.setattr(imf, "render_movie", lambda snaps, outdir, **kw: None)
    monkeypatch.setattr(imf, "ffmpeg_available", lambda: False)
    return calls


def test_imaging_pass_writes_frames_products_and_manifest(store, settings, fake_imaging):
    result = rf._render_imaging(store, settings)
    root = settings.store_root / "figures" / "imaging"
    assert result["n_frames_rendered"] == 2
    assert (root / "latest@1x.png").is_file()
    assert (root / "latest@2x.png").is_file()
    assert (root / "strip24h@1x.png").is_file()
    assert (root / "strip24h@2x.png").is_file()

    frames = sorted((root / "frames").glob("*@1x.png"))
    assert len(frames) == 2
    for png in frames:
        assert (root / "frames" / f"{png.name[:-len('@1x.png')]}.npz").is_file()

    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["cal_file"] == "cal_sep03peak_core17.h5"
    assert manifest["antennas"] == [9, 10, 15]
    assert manifest["latest"]["file_1x"] == "latest@1x.png"
    assert manifest["latest"]["ts"].endswith("Z")
    assert manifest["strip"]["file_2x"] == "strip24h@2x.png"
    assert manifest["strip"]["n"] >= 1
    # ffmpeg reported absent -> movie file null, but fps is still served.
    assert manifest["movie"] == {"file": None, "fps": imf.MOVIE_FPS}
    assert manifest["psf_ceiling_snr"] is None
    assert [s["name"] for s in manifest["sources"]] == list(imf.SOURCES)


def test_imaging_pass_is_incremental(store, settings, fake_imaging):
    rf._render_imaging(store, settings)
    first_calls = list(fake_imaging)
    second = rf._render_imaging(store, settings)
    # The second pass resumes AFTER the newest cached integration, so it never
    # re-images what is already on disk.
    assert fake_imaging[1][0] > first_calls[0][0]
    assert second["n_frames_cached"] == 4
    frames = sorted((settings.store_root / "figures" / "imaging" / "frames").glob("*@1x.png"))
    assert len(frames) == 4


def test_imaging_pass_bounds_one_job_to_max_frames(store, settings, monkeypatch):
    """A cold start images at most MAX_IMAGING_FRAMES integrations, oldest first."""
    asked: list[tuple[float, float]] = []

    def fake_window(store_, settings_, t0, t1, *, config=None, **kw):
        asked.append((t0, t1))
        return []

    monkeypatch.setattr(imf, "imaging_config", lambda settings: FakeConfig())
    monkeypatch.setattr(imf, "image_window", fake_window)
    monkeypatch.setattr(imf, "ffmpeg_available", lambda: False)
    rf._render_imaging(store, settings)
    t0, t1 = asked[0]
    assert t1 - t0 == pytest.approx(rf.MAX_IMAGING_FRAMES * imf.INTEGRATION_S, rel=1e-6)
    # Oldest first: the range starts at the 24 h window edge, not at "now".
    assert t0 < time.time() - 23 * 3600


def test_imaging_pass_advances_past_a_data_gap(store, settings, monkeypatch):
    """Every step skipped (a gap) must still move the resume point forward."""
    asked: list[tuple[float, float]] = []

    def fake_window(store_, settings_, t0, t1, *, config=None, **kw):
        asked.append((t0, t1))
        return []

    monkeypatch.setattr(imf, "imaging_config", lambda settings: FakeConfig())
    monkeypatch.setattr(imf, "image_window", fake_window)
    monkeypatch.setattr(imf, "ffmpeg_available", lambda: False)
    rf._render_imaging(store, settings)
    rf._render_imaging(store, settings)
    assert asked[1][0] == pytest.approx(asked[0][1], rel=1e-9)


def test_imaging_pass_skips_cleanly_without_a_deployed_cal(store, settings, monkeypatch):
    def boom(settings_):
        raise imf.ImagingUnavailable("no cal on record")

    monkeypatch.setattr(imf, "imaging_config", boom)
    result = rf._render_imaging(store, settings)
    assert "skipped" in result


def test_expire_frames_drops_old_ones(settings, tmp_path):
    frames = tmp_path / "frames"
    frames.mkdir()
    now = time.time()
    fresh = int(now - 3600)
    stale = int(now - 9 * 86400)
    for ts in (fresh, stale):
        (frames / f"{ts}@1x.png").write_bytes(b"png")
        (frames / f"{ts}.npz").write_bytes(b"npz")
    assert rf._expire_frames(frames, now, imf.FRAME_RETENTION_DAYS) == 1
    assert rf._cached_frame_ts(frames) == [fresh]


def test_strip_selection_is_one_frame_per_slot():
    t1 = 1788925720.0
    t0 = t1 - 24 * 3600
    # Three integrations inside each of two 30-min slots.
    ts_list = [int(t1 - 300 - k * 140) for k in range(3)]
    ts_list += [int(t1 - 1900 - k * 140) for k in range(3)]
    chosen = rf._strip_selection(sorted(ts_list), t0, t1)
    assert len(chosen) == 2
    assert all(ts in ts_list for ts in chosen)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_render_movie_writes_an_mp4(tmp_path):
    snaps = [synthetic_snapshot(1788835440.0 + 140 * k) for k in range(3)]
    out = imf.render_movie(snaps, tmp_path, fps=2)
    assert out is not None
    assert out.name == "allsky24h.mp4"
    assert out.stat().st_size > 0
