"""``FigureScheduler`` submits ``render_figures`` jobs (never renders itself),
and ``jobs.render_figures`` (the moved renderer) renders every (kind, set,
ref) into the store's figures/ tree, writes atomic manifests, skips a set
when nothing changed, and stays memory-bounded (``vis_avg8`` only, reduced
shard by shard -- never ``vis_full``, never the whole 24 h window at once)."""

from __future__ import annotations

import json
import time

import numpy as np
import pytest

from casm_monitor.collectors import rowmap
from casm_monitor.collectors import vis as visc
from casm_monitor.collectors.base import CollectorContext
from casm_monitor.collectors.figures import RENDER_FIGURES_KIND, FigureScheduler
from casm_monitor.figures import vis_figures as vf
from casm_monitor.jobs import render_figures as rf
from casm_monitor.store import ShardWriter, Store
from casm_monitor.web import vis as webvis

OBS = "2026-09-04-16:43:47"
LAYOUT = (
    "antenna,x,y,z,snap,adc,packet_idx,functional,include_in_beamforming,row,col\n"
    "1,0.0,0.0,0.0,0,0,0,1,0,N01,E1\n"
    "3,120.0,0.0,0.1,0,2,2,1,1,N01,E3\n"
    "4,0.0,90.0,0.2,0,3,3,1,1,N02,E1\n"
)
INPUTS = [0, 2, 3]
N_BL = 6
N_SAMPLES = 12
DT_S = 900.0


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


def _seed(settings, store, times):
    shards = ShardWriter(store, settings.shards_root)
    nchan = visc.AVG_NCHAN
    arr = np.stack([_frame(1.0 + 0.01 * k, nchan) for k in range(len(times))]).astype(np.complex64)
    shards.write(
        visc.STREAM_AVG8,
        arr,
        t0=times[0],
        t1=times[-1],
        meta={
            "obs": OBS,
            "inputs": INPUTS,
            "t": list(times),
            "chan_avg": visc.CHAN_AVG,
            "freq_top_mhz": rowmap.FREQ_TOP_MHZ - 0.5 * (visc.CHAN_AVG - 1) * rowmap.CHAN_BW_MHZ,
            "chan_bw_mhz": rowmap.CHAN_BW_MHZ * visc.CHAN_AVG,
            "freq_order": "descending",
            "flags": {"first_of_file": [False] * len(times)},
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
        int_idx=len(times) - 1,
        first_of_file=False,
    )


@pytest.fixture
def seeded_store(settings, layout):
    store = Store(settings.db_path, store_root=settings.store_root)
    now = time.time()
    times = [now - (N_SAMPLES - 1 - k) * DT_S for k in range(N_SAMPLES)]
    _seed(settings, store, times)
    yield store, times
    store.close()


# -- FigureScheduler: submits, never renders --------------------------------
def test_scheduler_submits_render_figures_job(settings, store):
    ctx = CollectorContext(settings=settings, store=store, shards=ShardWriter(store))
    FigureScheduler(settings).collect(ctx)

    jobs = store.query("SELECT kind, params, state FROM jobs")
    assert len(jobs) == 1
    assert jobs[0]["kind"] == RENDER_FIGURES_KIND
    params = json.loads(jobs[0]["params"])
    assert set(params["targets"]) == {"vis", "snaps", "imaging"}
    assert params["reason"] == "scheduled"
    job_id = store.latest_scalar("figures.scheduled_job_id")
    assert job_id is not None
    # No figures/ tree written -- the scheduler never touches matplotlib.
    assert not (settings.store_root / "figures").exists()


def test_scheduler_refuses_within_30_min(settings, store):
    ctx = CollectorContext(settings=settings, store=store, shards=ShardWriter(store))
    scheduler = FigureScheduler(settings)
    scheduler.collect(ctx)
    scheduler.collect(ctx)

    jobs = store.query("SELECT id FROM jobs WHERE kind = ?", (RENDER_FIGURES_KIND,))
    assert len(jobs) == 1
    _ts, values = store.series("figures.scheduled_skipped")
    assert 1 in values


def test_scheduler_refuses_while_pending_even_after_30_min(settings, store, monkeypatch):
    """A slot claimed 31 min ago does not mean submit again if one is queued."""
    ctx = CollectorContext(settings=settings, store=store, shards=ShardWriter(store))
    scheduler = FigureScheduler(settings)
    now = time.time()
    scheduler.collect(ctx)
    monkeypatch.setattr(time, "time", lambda: now + 2000.0)
    scheduler.collect(ctx)
    jobs = store.query("SELECT id FROM jobs WHERE kind = ?", (RENDER_FIGURES_KIND,))
    # refuse_if_pending still fires (the first job is still queued): no second row.
    assert len(jobs) == 1


def test_scheduler_submits_snaps_only_on_new_board_read(settings, store, monkeypatch):
    """The 30 min scheduled job already covers ``snaps`` -- the fast path only
    fires once that job is no longer pending (``refuse_if_pending`` is shared
    across both, same ``render_figures`` kind) but its 30 min slot is not yet
    due again, i.e. exactly the gap a board read lands in between passes."""
    from casm_monitor.jobs import snap_read as snap_read_job

    monkeypatch.setattr(
        snap_read_job,
        "latest_reads",
        lambda store_: {"192.168.120.52": {"ts": time.time()}},
    )
    ctx = CollectorContext(settings=settings, store=store, shards=ShardWriter(store))
    scheduler = FigureScheduler(settings)
    scheduler.collect(ctx)  # the first (scheduled) job claims the 30 min slot

    first = store.query("SELECT id FROM jobs WHERE kind = ?", (RENDER_FIGURES_KIND,))
    assert len(first) == 1
    store.execute(
        "UPDATE jobs SET state = 'done', finished = ? WHERE id = ?",
        (time.time(), first[0]["id"]),
    )

    scheduler.collect(ctx)  # slot still held (< 30 min) -> only the fast path can fire

    jobs = store.query("SELECT kind, params FROM jobs ORDER BY id")
    kinds = [(j["kind"], json.loads(j["params"])["targets"]) for j in jobs]
    assert (RENDER_FIGURES_KIND, ["snaps"]) in kinds


# -- the moved renderer: jobs.render_figures --------------------------------
def test_render_figures_renders_and_writes_manifests(settings, seeded_store, monkeypatch):
    store, times = seeded_store
    # No deployed cal in this settings fixture -> only raw/sun are expected.
    monkeypatch.setattr(
        "casm_monitor.jobs.render_figures.load_deployed_cal",
        lambda settings: (_ for _ in ()).throw(RuntimeError("no cal in tests")),
    )
    result = rf._render_vis(store, settings)
    assert result["peak_rss_mb"] > 0

    root = settings.store_root / "figures" / "vis"
    for set_name in vf.SETS:
        for ref in ("raw", "sun"):
            manifest_path = root / set_name / ref / "manifest.json"
            assert manifest_path.is_file(), manifest_path
            manifest = json.loads(manifest_path.read_text())
            assert manifest["set"] == set_name
            assert manifest["ref"] == ref
            assert manifest["n_integrations"] == N_SAMPLES
            assert set(manifest["kinds"]) == set(vf.KINDS)
            for kind, files in manifest["files"].items():
                for suffix, name in files.items():
                    png_path = root / set_name / ref / name
                    assert png_path.is_file()
                    assert png_path.stat().st_size > 0
        # cal was made unavailable -> no cal directory at all.
        assert not (root / set_name / "cal").exists()

    n_rendered = store.latest_scalar("figures.vis.n_rendered")
    assert n_rendered is not None
    assert n_rendered["value"] == 2 * len(vf.KINDS) * 2  # 2 sets * kinds * 2 refs
    peak = store.latest_scalar("figures.vis.peak_rss_mb")
    assert peak is not None and peak["value"] > 0

    # No stray .tmp- files left behind (atomic rename happened).
    leftovers = list(root.rglob(".tmp-*"))
    assert leftovers == []


def test_render_figures_skips_when_nothing_new(settings, seeded_store, monkeypatch):
    store, times = seeded_store
    monkeypatch.setattr(
        "casm_monitor.jobs.render_figures.load_deployed_cal",
        lambda settings: (_ for _ in ()).throw(RuntimeError("no cal in tests")),
    )
    rf._render_vis(store, settings)

    root = settings.store_root / "figures" / "vis"
    manifest_path = root / "wired" / "raw" / "manifest.json"
    first_manifest = json.loads(manifest_path.read_text())
    first_mtime = manifest_path.stat().st_mtime_ns

    # Second pass, same cached data: every set should be reported skipped and
    # no manifest should be rewritten.
    rf._render_vis(store, settings)
    second_manifest = json.loads(manifest_path.read_text())
    assert second_manifest == first_manifest
    assert manifest_path.stat().st_mtime_ns == first_mtime

    skipped = store.series("figures.vis.skipped")
    assert len(skipped) >= 2  # one per set, this second pass


def test_render_figures_failure_is_isolated_and_logged(settings, seeded_store, monkeypatch):
    store, times = seeded_store
    monkeypatch.setattr(
        "casm_monitor.jobs.render_figures.load_deployed_cal",
        lambda settings: (_ for _ in ()).throw(RuntimeError("no cal in tests")),
    )
    real_render_kind = vf.render_kind

    def flaky(store_, settings_, kind, set_name, ref, *, window=None):
        if kind == "autos" and ref == "sun":
            raise RuntimeError("synthetic failure")
        return real_render_kind(store_, settings_, kind, set_name, ref, window=window)

    monkeypatch.setattr("casm_monitor.figures.vis_figures.render_kind", flaky)
    rf._render_vis(store, settings)

    n_failed = store.latest_scalar("figures.vis.n_failed")
    assert n_failed["value"] >= 2  # one per set (live, wired)
    events = store.events(kind="figures_render_failed")
    assert len(events) >= 2
    # Everything else in that set still rendered.
    root = settings.store_root / "figures" / "vis"
    manifest = json.loads((root / "wired" / "sun" / "manifest.json").read_text())
    assert "autos" not in manifest["kinds"]
    assert "matrix_amp" in manifest["kinds"]


def test_render_figures_not_enough_data_is_a_placeholder(settings, layout):
    """Fewer than MIN_INTEGRATIONS avg8 samples: placeholder PNGs, no crash,
    and (crucially) NEVER a fall back to vis_full."""
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        now = time.time()
        times = [now - (2 - k) * DT_S for k in range(3)]  # < MIN_INTEGRATIONS
        _seed(settings, store, times)
        result = rf._render_vis(store, settings)
        assert result["n_placeholder"] > 0
        root = settings.store_root / "figures" / "vis"
        manifest = json.loads((root / "live" / "raw" / "manifest.json").read_text())
        assert manifest["n_integrations"] == 0
        assert manifest["stream"] == "none"
    finally:
        store.close()


# -- memory bound: the reduced cube shape ------------------------------------
def test_load_window_reduces_cube_shape(settings, seeded_store):
    """Never the whole window: at most MAX_TIME_BINS x NCHAN_FIG per baseline."""
    store, times = seeded_store
    window = vf.load_window(store, settings, "wired")
    assert window.z.shape[0] <= vf.MAX_TIME_BINS
    assert window.z.shape[0] == N_SAMPLES  # one sample per bin here, none collide
    assert window.z.shape[-1] == vf.NCHAN_FIG
    assert window.freq_mhz.shape[0] == vf.NCHAN_FIG
    assert window.stream == visc.STREAM_AVG8


def test_load_window_raises_not_enough_data_below_threshold(settings, layout):
    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        now = time.time()
        times = [now - (2 - k) * DT_S for k in range(3)]
        _seed(settings, store, times)
        with pytest.raises(vf.NotEnoughData):
            vf.load_window(store, settings, "wired")
    finally:
        store.close()
