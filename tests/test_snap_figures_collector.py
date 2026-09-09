"""``jobs.render_figures``'s SNAPs half: renders both input sets, writes one
manifest per set, skips when unchanged, and re-renders only
``spectra_board`` when a new board read lands without any new Kafka data."""

from __future__ import annotations

import json
import time

import pytest

from casm_monitor.collectors import rowmap
from casm_monitor.figures import snap_figures as sf
from casm_monitor.jobs import render_figures as rf
from casm_monitor.store import Store

from tests.test_snap_figures import LAYOUT, SNAP_MAP, seed_board_read, seed_kafka_sub


@pytest.fixture
def layouts(tmp_path, monkeypatch):
    layout = tmp_path / "layout.csv"
    snap_map = tmp_path / "snap_map.csv"
    layout.write_text(LAYOUT)
    snap_map.write_text(SNAP_MAP)
    monkeypatch.setattr(rowmap, "LAYOUT_CSV", layout)
    monkeypatch.setattr(rowmap, "SNAP_MAP_CSV", snap_map)
    return layout, snap_map


def test_render_figures_renders_snap_manifests(settings, layouts):
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        now = time.time()
        seed_kafka_sub(store, settings, [0, 4, 24], now)
        seed_board_read(store, settings, "192.168.120.52", now)
        result = rf._render_snaps(store, settings)
        assert result["peak_rss_mb"] > 0

        root = settings.store_root / "figures" / "snaps"
        for set_name in sf.SETS:
            manifest_path = root / set_name / "manifest.json"
            assert manifest_path.is_file()
            manifest = json.loads(manifest_path.read_text())
            assert manifest["set"] == set_name
            assert set(manifest["kinds"]) == set(sf.KINDS)
            assert manifest["board_read_ts"] == pytest.approx(now)
            for kind, files in manifest["files"].items():
                for suffix, name in files.items():
                    png_path = root / set_name / name
                    assert png_path.is_file()
                    assert png_path.stat().st_size > 0

        leftovers = list(root.rglob(".tmp-*")) + list(root.rglob(".entry-*"))
        assert leftovers == []
        peak = store.latest_scalar("figures.snaps.peak_rss_mb")
        assert peak is not None and peak["value"] > 0
    finally:
        store.close()


def test_render_figures_skips_when_nothing_new(settings, layouts):
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        now = time.time()
        seed_kafka_sub(store, settings, [0, 4, 24], now)
        rf._render_snaps(store, settings)

        root = settings.store_root / "figures" / "snaps" / "beamforming"
        manifest_path = root / "manifest.json"
        first = json.loads(manifest_path.read_text())
        first_mtime = manifest_path.stat().st_mtime_ns

        rf._render_snaps(store, settings)
        second = json.loads(manifest_path.read_text())
        assert second == first
        assert manifest_path.stat().st_mtime_ns == first_mtime

        skipped = store.series("figures.snaps.skipped")
        assert len(skipped) >= 2  # one per set, this second pass
    finally:
        store.close()


def test_render_figures_rerenders_only_spectra_board_on_new_read(settings, layouts):
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        now = time.time()
        seed_kafka_sub(store, settings, [0, 4, 24], now)
        rf._render_snaps(store, settings)

        root = settings.store_root / "figures" / "snaps" / "beamforming"
        manifest_path = root / "manifest.json"
        first = json.loads(manifest_path.read_text())
        other_kind = "spectra_correlator"
        other_bytes_before = (root / first["files"][other_kind]["1x"]).read_bytes()

        # A board read lands, but no new Kafka data -- only spectra_board
        # should be re-rendered; the other three kinds' PNGs are untouched.
        seed_board_read(store, settings, "192.168.120.52", time.time())
        rf._render_snaps(store, settings)

        second = json.loads(manifest_path.read_text())
        assert second["board_read_ts"] is not None
        assert second["t1_kafka"] == first["t1_kafka"]
        other_bytes_after = (root / second["files"][other_kind]["1x"]).read_bytes()
        assert other_bytes_after == other_bytes_before
        # spectra_board's own file did change (a board read now exists).
        board_png = root / second["files"]["spectra_board"]["1x"]
        assert board_png.is_file() and board_png.stat().st_size > 0
    finally:
        store.close()
