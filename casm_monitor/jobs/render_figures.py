"""The ``render_figures`` job: renders the Vis, SNAPs, Imaging and
Candidates-funnel PNGs.

Moved here from ``FigureCollector`` (2026-09-09): the collector process runs
under ``MemoryMax=8G`` and rendering 16 figures concurrently off a whole 24 h
``vis_avg8``/``vis_full`` window put it in an OOM crash loop (21:23-22:16
PDT) that also starved every other collector. This job runs inside
``casm-monitor-jobs.service`` (``MemoryMax=256G``, one job at a time), so a
slow or memory-heavy render pass no longer competes with the cheap collectors
for the same cgroup.

Two things make this safe where the collector was not:

* :func:`casm_monitor.figures.vis_figures.load_window` never reads
  ``vis_full`` and never concatenates the whole window into one array -- it
  accumulates a small (time-bins x channels) reduction shard by shard (see
  that module's docstring). Below :data:`~casm_monitor.figures.vis_figures.
  MIN_INTEGRATIONS` samples it raises ``NotEnoughData`` and every kind in
  that set gets a "not enough data yet" placeholder instead of a render.
* rendering itself runs at most :data:`MAX_WORKERS` figures at a time (not
  16), so the accumulator plus a handful of in-flight ``Figure`` objects is
  the whole memory footprint, not one shard's worth of figures all at once.

Peak RSS is recorded per target (``figures.vis.peak_rss_mb`` /
``figures.snaps.peak_rss_mb``, via ``resource.getrusage`` -- a
whole-process high-water mark, so the vis number is read right after the vis
target finishes and the snaps number after snaps finishes; the latter can
never be lower than the former since the mark never resets, but the process
never renders anything before the vis target in this job, so the vis-target
number is the true peak for that phase).
"""

from __future__ import annotations

import concurrent.futures
import io
import json
import logging
import os
import resource
import time
from pathlib import Path
from typing import Any

from ..config import Settings, load_settings
from ..store import Store
from ..store.shards import ensure_contained, safe_name
from ..web.vis import load_deployed_cal

log = logging.getLogger("casm_monitor.jobs.render_figures")

# Bounded, not the collector's 16: keeps a handful of Figure objects and their
# canvases in flight at once, not sixteen 24 h-window renders at a time.
# Raised 4 -> 8 alongside the 2026-09-08 imshow/single-render figure
# optimisation: the jobs unit's CPUQuota is 1600% (16 cores), so 8 concurrent
# renders is still well inside budget, and each render is now cheap enough
# (matrix figure well under the 15 s target on a synthetic 24-input cube,
# see bench_render.py) that the win from more parallel workers is real,
# not just more contention. RLIMIT_AS stays 16G regardless (kinds.py).
MAX_WORKERS = 8
BASE_REFS = ("raw", "sun")

TARGETS = ("vis", "snaps", "imaging", "cands")


def _vf():
    from ..figures import vis_figures
    return vis_figures


def _sf():
    from ..figures import snap_figures
    return snap_figures


def _peak_rss_mb() -> float:
    """Whole-process RSS high-water mark, MB (``ru_maxrss`` is KB on Linux)."""
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)


# -- Vis tab --------------------------------------------------------------
def _render_vis(store: Store, settings: Settings) -> dict[str, Any]:
    root = ensure_contained(
        Path(settings.store_root) / "figures" / "vis", settings.store_root
    )
    refs = list(BASE_REFS)
    try:
        load_deployed_cal(settings)
        refs.append("cal")
    except Exception as exc:  # depends on the live ledger/cal file
        log.info("render_figures: cal reference unavailable this pass: %s", exc)

    started = time.time()
    n_rendered = 0
    n_skipped = 0
    n_failed = 0
    n_placeholder = 0
    for set_name in _vf().SETS:
        try:
            window = _vf().load_window(store, settings, set_name)
        except _vf().NoData as exc:
            log.info("render_figures: no data for set=%s: %s", set_name, exc)
            continue
        except _vf().NotEnoughData as exc:
            log.info("render_figures: not enough data for set=%s: %s", set_name, exc)
            n_placeholder += _write_placeholder_set(root, set_name, refs, str(exc))
            continue
        watermark = _read_manifest(root, set_name, refs[0])
        if watermark is not None and _unchanged(watermark, window):
            store.put_scalar("figures.vis.skipped", 1, tags={"set": set_name})
            n_skipped += len(_vf().KINDS) * len(refs)
            continue
        rendered, failed = _render_vis_set(store, settings, root, set_name, refs, window)
        n_rendered += rendered
        n_failed += failed
    total_s = time.time() - started
    peak_rss_mb = _peak_rss_mb()
    store.put_scalar("figures.vis.render_s", round(total_s, 3))
    store.put_scalar("figures.vis.n_rendered", n_rendered)
    store.put_scalar("figures.vis.n_skipped", n_skipped)
    store.put_scalar("figures.vis.n_failed", n_failed)
    store.put_scalar("figures.vis.n_placeholder", n_placeholder)
    store.put_scalar("figures.vis.peak_rss_mb", peak_rss_mb)
    return {
        "render_s": round(total_s, 3),
        "n_rendered": n_rendered,
        "n_skipped": n_skipped,
        "n_failed": n_failed,
        "n_placeholder": n_placeholder,
        "peak_rss_mb": peak_rss_mb,
    }


def _write_placeholder_set(
    root: Path, set_name: str, refs: list[str], message: str
) -> int:
    """"Not enough data yet" for every (kind, ref) in this set -- still a manifest."""
    n = 0
    for ref in refs:
        entries = []
        for kind in _vf().KINDS:
            pngs = _vf().render_placeholder_kind(message, set_name, kind)
            outdir = ensure_contained(root / safe_name(set_name) / safe_name(ref), root)
            outdir.mkdir(parents=True, exist_ok=True)
            for suffix, data in pngs.items():
                _atomic_write(outdir / f"{kind}@{suffix}.png", data)
            n += 1
            entries.append({
                "kind": kind,
                "files": {suf: f"{kind}@{suf}.png" for suf in pngs},
                "t0": time.time(),
                "t1": time.time(),
                "n_integrations": 0,
                "stream": "none",
                "obs": None,
            })
        _write_manifest(root, set_name, ref, entries)
    return n


# Rendered first within a set: the default view (``matrix_phase`` + ``autos``
# for the ``raw`` reference) so a job that times out partway (``render_figures``
# now 1800 s, but 33 renders/set is still a lot -- kinds.py) still leaves the
# page most people load fresh, not whatever kinds happened to sort first.
_PRIORITY_KINDS = ("matrix_phase", "autos")


def _prioritized_vis_jobs(refs: list[str], kinds: tuple[str, ...]) -> list[tuple[str, str]]:
    jobs = [(kind, ref) for ref in refs for kind in kinds]
    return sorted(
        jobs,
        key=lambda job: (0 if job[1] == "raw" else 1, 0 if job[0] in _PRIORITY_KINDS else 1),
    )


def _seed_vis_entries(root: Path, set_name: str, ref: str) -> dict[str, dict[str, Any]]:
    """Existing manifest entries, keyed by kind, so a kind this pass doesn't
    reach (timeout, or simply not requested) keeps its last-good entry rather
    than the manifest silently losing it."""
    manifest = _read_manifest(root, set_name, ref)
    if not manifest:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for kind, files in (manifest.get("files") or {}).items():
        out[kind] = {
            "kind": kind, "files": files, "t0": manifest.get("t0"), "t1": manifest.get("t1"),
            "n_integrations": manifest.get("n_integrations"), "stream": manifest.get("stream"),
            "obs": manifest.get("obs"),
        }
    return out


def _render_vis_set(
    store: Store, settings: Settings, root: Path, set_name: str, refs: list[str], window: "Any"
) -> tuple[int, int]:
    vf = _vf()
    jobs = _prioritized_vis_jobs(refs, vf.KINDS)
    by_ref: dict[str, dict[str, dict[str, Any]]] = {
        ref: _seed_vis_entries(root, set_name, ref) for ref in refs
    }
    rendered = 0
    failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_render_vis_one, store, settings, set_name, ref, kind, root, window): (kind, ref)
            for kind, ref in jobs
        }
        for fut in concurrent.futures.as_completed(futures):
            kind, ref = futures[fut]
            try:
                entry = fut.result()
            except Exception as exc:  # never let one figure kill the pass
                failed += 1
                log.exception(
                    "render_figures: render failed for %s/%s/%s", set_name, ref, kind
                )
                store.add_event(
                    "figures_render_failed",
                    severity="warn",
                    subject=f"{set_name}/{ref}/{kind}",
                    detail={"error": str(exc)},
                )
                continue
            rendered += 1
            by_ref[ref][kind] = entry
            # Written after EVERY kind, not once at the end of the whole set:
            # a job that hits its timeout partway through still leaves a
            # manifest reflecting whatever finished, not nothing.
            _write_manifest(root, set_name, ref, list(by_ref[ref].values()))
    return rendered, failed


def _render_vis_one(
    store: Store, settings: Settings, set_name: str, ref: str, kind: str, root: Path, window: "Any"
) -> dict[str, Any]:
    vf = _vf()
    started = time.time()
    pngs, info = vf.render_kind(store, settings, kind, set_name, ref, window=window)
    render_s = time.time() - started
    store.put_scalar(
        "figures.vis.render_s", round(render_s, 3),
        tags={"kind": kind, "set": set_name, "ref": ref},
    )
    outdir = ensure_contained(root / safe_name(set_name) / safe_name(ref), root)
    outdir.mkdir(parents=True, exist_ok=True)
    for suffix, data in pngs.items():
        _atomic_write(outdir / f"{kind}@{suffix}.png", data)
    return {
        "kind": kind,
        "files": {suf: f"{kind}@{suf}.png" for suf in pngs},
        "t0": info["t0"],
        "t1": info["t1"],
        "n_integrations": info["n_integrations"],
        "stream": info["stream"],
        "obs": info["obs"],
        "render_s": round(render_s, 3),
    }


def _write_manifest(root: Path, set_name: str, ref: str, entries: list[dict[str, Any]]) -> None:
    outdir = ensure_contained(root / safe_name(set_name) / safe_name(ref), root)
    outdir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "rendered_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "set": set_name,
        "ref": ref,
        "t0": min(e["t0"] for e in entries),
        "t1": max(e["t1"] for e in entries),
        "n_integrations": max(e["n_integrations"] for e in entries),
        "stream": entries[0]["stream"],
        "obs": entries[0]["obs"],
        "files": {e["kind"]: e["files"] for e in entries},
        "kinds": sorted(e["kind"] for e in entries),
    }
    path = outdir / "manifest.json"
    tmp = outdir / f".tmp-{os.getpid()}-manifest.json"
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    os.replace(tmp, path)


def _read_manifest(root: Path, set_name: str, ref: str) -> dict[str, Any] | None:
    path = root / set_name / ref / "manifest.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _unchanged(manifest: dict[str, Any], window: "Any") -> bool:
    if not len(window.times):
        return False
    return (
        manifest.get("t1") == float(window.times[-1])
        and manifest.get("n_integrations") == len(window.times)
    )


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".tmp-{os.getpid()}-{path.name}")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    dfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


# -- SNAPs tab --------------------------------------------------------------
def _render_snaps(store: Store, settings: Settings) -> dict[str, Any]:
    """Same shape as the vis pass, ported unchanged from ``FigureCollector``.

    The SNAPs figures read the Kafka bandpass history (already sub-sampled,
    see :mod:`casm_monitor.collectors.kafka_bp`) and one board read's 12x4096
    spectra -- not the multi-hundred-MB vis window -- so no reduction was
    needed here to hit the memory target; this still runs in the job (not the
    collector) and at :data:`MAX_WORKERS` concurrency for the same reason: it
    shares the OOM'd process with the vis half in the old collector.
    """
    sf = _sf()
    root = ensure_contained(
        Path(settings.store_root) / "figures" / "snaps", settings.store_root
    )
    try:
        boards = sf.board_table()
    except OSError as exc:
        log.info("render_figures: snap layout unreadable this pass: %s", exc)
        return {"skipped": "layout unreadable"}
    board_read_ts = _latest_board_read_ts(store)
    kafka_t1 = _newest_kafka_t1(store)
    started = time.time()
    n_rendered = 0
    n_failed = 0
    for set_name in sf.SETS:
        outdir = ensure_contained(root / safe_name(set_name), root)
        manifest = _read_json(outdir / "manifest.json")
        kafka_changed = kafka_t1 is not None and kafka_t1 != (manifest or {}).get("t1_kafka")
        board_changed = board_read_ts is not None and board_read_ts != (manifest or {}).get(
            "board_read_ts"
        )
        if manifest is not None and not kafka_changed and not board_changed:
            store.put_scalar("figures.snaps.skipped", 1, tags={"set": set_name})
            continue
        kinds = list(sf.KINDS) if (manifest is None or kafka_changed) else []
        if board_changed and "spectra_board" not in kinds:
            kinds.append("spectra_board")
        rendered, failed = _render_snap_set(
            store, settings, outdir, set_name, kinds, boards, manifest, board_read_ts, kafka_t1
        )
        n_rendered += rendered
        n_failed += failed
    total_s = time.time() - started
    peak_rss_mb = _peak_rss_mb()
    store.put_scalar("figures.snaps.render_s", round(total_s, 3))
    store.put_scalar("figures.snaps.n_rendered", n_rendered)
    store.put_scalar("figures.snaps.n_failed", n_failed)
    store.put_scalar("figures.snaps.peak_rss_mb", peak_rss_mb)
    return {
        "render_s": round(total_s, 3),
        "n_rendered": n_rendered,
        "n_failed": n_failed,
        "peak_rss_mb": peak_rss_mb,
    }


def _seed_snap_entries(old_manifest: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Existing manifest entries, keyed by kind (same idea as the vis side's
    ``_seed_vis_entries``): a kind this pass doesn't reach keeps its
    last-good entry instead of the manifest silently losing it."""
    if not old_manifest:
        return {}
    files_by_kind = old_manifest.get("files") or {}
    return {
        kind: {
            "files": files, "t0": old_manifest.get("t0"), "t1": old_manifest.get("t1"),
            "n_frames": old_manifest.get("n_frames"),
        }
        for kind, files in files_by_kind.items()
    }


def _render_snap_set(
    store: Store,
    settings: Settings,
    outdir: Path,
    set_name: str,
    kinds: list[str],
    boards: list[dict[str, Any]],
    old_manifest: dict[str, Any] | None,
    board_read_ts: float | None,
    kafka_t1: float | None,
) -> tuple[int, int]:
    if not kinds:
        return 0, 0
    outdir.mkdir(parents=True, exist_ok=True)
    entries = _seed_snap_entries(old_manifest)
    rendered = 0
    failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_render_snap_kind, store, settings, outdir, set_name, kind, boards): kind
            for kind in kinds
        }
        for fut in concurrent.futures.as_completed(futures):
            kind = futures[fut]
            try:
                entry = fut.result()
            except Exception as exc:
                failed += 1
                log.exception("render_figures: snap render failed for %s/%s", set_name, kind)
                store.add_event(
                    "figures_render_failed",
                    severity="warn",
                    subject=f"snaps/{set_name}/{kind}",
                    detail={"error": str(exc)},
                )
                continue
            rendered += 1
            entries[kind] = entry
            # Written after EVERY kind, not once at the end of the whole set
            # (see ``_render_vis_set``'s identical reasoning): a job that
            # hits its timeout partway through still leaves a manifest
            # reflecting whatever finished.
            _write_snap_manifest(outdir, set_name, entries, board_read_ts, kafka_t1)
    return rendered, failed


def _render_snap_kind(
    store: Store,
    settings: Settings,
    outdir: Path,
    set_name: str,
    kind: str,
    boards: list[dict[str, Any]],
) -> dict[str, Any]:
    sf = _sf()
    started = time.time()
    pngs, info = sf.render_kind(store, settings, kind, set_name, boards=boards)
    render_s = time.time() - started
    store.put_scalar(
        "figures.snaps.render_s", round(render_s, 3), tags={"kind": kind, "set": set_name}
    )
    for suffix, data in pngs.items():
        _atomic_write(outdir / f"{kind}@{suffix}.png", data)
    return {
        "files": {suf: f"{kind}@{suf}.png" for suf in pngs},
        "t0": info.get("t0"),
        "t1": info.get("t1"),
        "n_frames": info.get("n_frames"),
        "render_s": round(render_s, 3),
    }


def _write_snap_manifest(
    outdir: Path,
    set_name: str,
    entries: dict[str, dict[str, Any]],
    board_read_ts: float | None,
    kafka_t1: float | None,
) -> None:
    if not entries:
        return
    t0 = t1 = n_frames = None
    files: dict[str, Any] = {}
    for kind, entry in entries.items():
        files[kind] = entry["files"]
        if entry.get("t0") is not None:
            t0 = entry["t0"] if t0 is None else min(t0, entry["t0"])
        if entry.get("t1") is not None:
            t1 = entry["t1"] if t1 is None else max(t1, entry["t1"])
        if entry.get("n_frames") is not None:
            n_frames = max(n_frames or 0, entry["n_frames"])
    manifest = {
        "rendered_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "set": set_name,
        "t0": t0,
        "t1": t1,
        "n_frames": n_frames,
        "board_read_ts": board_read_ts,
        "t1_kafka": kafka_t1,
        "files": files,
        "kinds": sorted(files),
    }
    path = outdir / "manifest.json"
    tmp = outdir / f".tmp-{os.getpid()}-manifest.json"
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _latest_board_read_ts(store: Store) -> float | None:
    from .snap_read import latest_reads

    reads = latest_reads(store)
    if not reads:
        return None
    return max((float(s.get("ts") or 0.0) for s in reads.values()), default=None)


def _newest_kafka_t1(store: Store) -> float | None:
    rows = store.query(
        "SELECT MAX(t1) AS t FROM shards WHERE stream IN ('kafka_bp_sub', 'kafka_bp_full')"
    )
    value = rows[0]["t"] if rows else None
    return float(value) if value is not None else None


# -- Imaging tab ------------------------------------------------------------
# The imaging pass is INCREMENTAL at the integration level, unlike the vis and
# snaps passes (which re-render a whole set when its watermark moves): one
# all-sky snapshot costs ~5 s (MEASURED 2026-09-09, plan.md), so re-imaging a
# whole 24 h day (~629 integrations) every half hour would be an hour of CPU
# per pass. Instead every integration is imaged ONCE into a frame cache
# (``figures/imaging/frames/<unix>@1x.png`` plus a ``.npz`` of the image), and
# the latest/strip/movie products are rebuilt from that cache, which is cheap.
# A pass images at most MAX_IMAGING_FRAMES new integrations, oldest first, so
# a long outage catches up over several passes instead of one job that blows
# its timeout.
MAX_IMAGING_FRAMES = 64
#: Cold start / long outage: when the cache is more than
#: :data:`COLD_START_LAG_S` behind, a pass images up to this many integrations
#: instead. 256 frames x 2.9 s/frame (MEASURED 2026-09-09: 6 snapshots in 17.3 s
#: on the live 17-antenna deployed set, 241 px, freq_avg 32, 8 workers) is
#: ~12 min, so the whole job -- vis + snaps + imaging products + the ~5.5 s
#: per source cutout -- stays inside its 1800 s timeout (kinds.py), while a day
#: of backlog now catches up in three passes instead of ten.
MAX_IMAGING_FRAMES_COLD = 256
COLD_START_LAG_S = 6 * 3600.0

#: PSF ceiling budget. ``psf_for_result`` replays the whole geometry on a
#: simulated point source, which is a second beamform per cutout; the cost is
#: measured on the first pass that computes one and remembered in
#: ``psf_cache.json``. MEASURED 2026-09-09 on the deployed 17-antenna set: 0.1 s
#: for a 51 px cutout, i.e. far inside the budget, so it is computed every pass;
#: the skipping path exists for a future larger grid, not for today's cost. Above this budget the ceilings are recomputed only every
#: PSF_PASS_INTERVAL-th pass (3 h at the 30-min job cadence) and the cached
#: values are reported in between -- they only change when the array config
#: does, which is exactly what the fingerprint tracks.
PSF_COST_BUDGET_S = 120.0
PSF_PASS_INTERVAL = 6


def _if():
    from ..figures import imaging_figures
    return imaging_figures


def _frames_dir(root: Path) -> Path:
    return ensure_contained(root / "frames", root)


def _cached_frame_ts(frames_dir: Path) -> list[int]:
    """Unix timestamps of COMPLETE cached frames (PNG *and* npz), ascending.

    A frame missing either half is not listed: the history route promises only
    files that actually exist on disk, and the strip/movie need the npz.
    """
    if not frames_dir.is_dir():
        return []
    out: list[int] = []
    for png in frames_dir.glob("*@1x.png"):
        stem = png.name[: -len("@1x.png")]
        if not stem.isdigit():
            continue
        if (frames_dir / f"{stem}.npz").is_file():
            out.append(int(stem))
    return sorted(out)


def _frame_halves(frames_dir: Path) -> dict[int, list[Path]]:
    """Every frame file on disk, grouped by timestamp -- halves included.

    Retention has to see a PNG whose npz never landed (and vice versa), or a
    crashed pass leaves an orphan that no expiry ever reaches (2026-09-09
    review, finding 10).
    """
    out: dict[int, list[Path]] = {}
    if not frames_dir.is_dir():
        return out
    for path in frames_dir.iterdir():
        name = path.name
        if name.endswith("@1x.png"):
            stem = name[: -len("@1x.png")]
        elif name.endswith(".npz"):
            stem = name[: -len(".npz")]
        else:
            continue
        if not stem.isdigit():
            continue
        out.setdefault(int(stem), []).append(path)
    return out


def _unlink_all(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink()
        except OSError:
            continue


def _expire_frames(frames_dir: Path, now: float, retention_days: float) -> int:
    """Delete cached frames older than the retention window; returns the count.

    Runs BEFORE the imaging configuration is resolved, so a pass that has no
    deployed cal to image with still keeps the cache inside its retention
    window (2026-09-09 review, finding 10).
    """
    cutoff = now - float(retention_days) * 86400.0
    n = 0
    for ts, paths in _frame_halves(frames_dir).items():
        if ts >= cutoff:
            continue
        _unlink_all(paths)
        n += 1
    return n


def _frame_fingerprint(frames_dir: Path, ts: int) -> str | None:
    """The config fingerprint a cached frame was imaged with, or None."""
    import numpy as np

    try:
        with np.load(frames_dir / f"{ts}.npz", allow_pickle=False) as z:
            return str(z["fingerprint"])
    except (OSError, ValueError, KeyError):
        return None


def _expire_stale_frames(frames_dir: Path, fingerprint: str) -> int:
    """Delete every frame not imaged with ``fingerprint``; returns the count.

    A frame made with a superseded cal or a different antenna set is not a
    frame of the configuration the manifest advertises, so it is neither shown
    (latest/strip/movie) nor kept (2026-09-09 review, finding 2). A frame
    whose npz is missing or unreadable goes too: its fingerprint is unknowable
    and it can never be drawn from anyway.
    """
    n = 0
    for ts, paths in _frame_halves(frames_dir).items():
        if _frame_fingerprint(frames_dir, ts) == fingerprint:
            continue
        _unlink_all(paths)
        n += 1
    return n


def _write_frame(frames_dir: Path, snap: dict[str, Any], fingerprint: str) -> int:
    """Cache one integration: the scrub PNG and the image itself.

    The npz is float32 (half the bytes of the float64 the imager returns, and
    a dirty image has nowhere near 7 significant digits of meaning) and holds
    everything the strip/movie need to redraw the frame without re-imaging it,
    plus the configuration fingerprint it was imaged with.
    """
    import numpy as np

    ts = int(round(float(snap["time_unix"])))
    _atomic_write(frames_dir / f"{ts}@1x.png", _if().render_frame(snap))
    names = list((snap.get("sources") or {}).keys())
    lma = (
        np.asarray([list(snap["sources"][n]) for n in names], dtype=np.float32)
        if names
        else np.zeros((0, 3), dtype=np.float32)
    )
    buf = io.BytesIO()
    np.savez_compressed(
        buf,
        time_unix=np.float64(float(snap["time_unix"])),
        image=np.asarray(snap["image"], dtype=np.float32),
        l_axis=np.asarray(snap["l_axis"], dtype=np.float32),
        m_axis=np.asarray(snap["m_axis"], dtype=np.float32),
        src_names=np.asarray(names, dtype="U16"),
        src_lma=lma,
        fingerprint=np.asarray(str(fingerprint)),
    )
    _atomic_write(frames_dir / f"{ts}.npz", buf.getvalue())
    return ts


def _load_frame(frames_dir: Path, ts: int) -> dict[str, Any] | None:
    """One cached frame back as an ``allsky_snapshots``-shaped dict."""
    import numpy as np

    path = frames_dir / f"{ts}.npz"
    try:
        with np.load(path, allow_pickle=False) as z:
            names = [str(n) for n in z["src_names"]]
            lma = np.asarray(z["src_lma"], dtype=float)
            return {
                "time_unix": float(z["time_unix"]),
                "image": np.asarray(z["image"], dtype=float),
                "l_axis": np.asarray(z["l_axis"], dtype=float),
                "m_axis": np.asarray(z["m_axis"], dtype=float),
                "sources": {n: tuple(float(x) for x in lma[i]) for i, n in enumerate(names)},
            }
    except (OSError, ValueError, KeyError):
        log.warning("render_figures: unreadable imaging frame %s", path)
        return None


def _strip_selection(frame_ts: list[int], t0: float, t1: float) -> list[int]:
    """One frame per 30-min slot across ``[t0, t1]``, nearest the slot centre."""
    imaging = _if()
    slot_s = imaging.STRIP_SLOT_S
    chosen: list[int] = []
    # The STRIP_MAX slots TILE [t1 - 24 h, t1] (48 x 30 min = exactly 24 h),
    # so the slot centres sit half a slot inside each end -- anchoring them on
    # t1 itself would leave the oldest half hour of the window unrepresented.
    for k in range(imaging.STRIP_MAX):
        centre = t1 - (imaging.STRIP_MAX - k - 0.5) * slot_s
        if centre + slot_s / 2.0 < t0:
            continue
        window = [ts for ts in frame_ts if abs(ts - centre) <= slot_s / 2.0]
        if not window:
            continue
        best = min(window, key=lambda ts: abs(ts - centre))
        if best not in chosen:
            chosen.append(best)
    return chosen


def _load_frames_one_at_a_time(frames_dir: Path, ts_list: list[int]) -> list[dict[str, Any]]:
    """Load the SELECTED frames, one npz at a time, skipping unreadable ones.

    The selection is always made on timestamps first (``_strip_selection`` /
    ``imaging_figures.movie_selection``), so this never opens more npz files
    than the product actually draws (2026-09-09 review, finding 9).
    """
    out: list[dict[str, Any]] = []
    for ts in ts_list:
        snap = _load_frame(frames_dir, ts)
        if snap is not None:
            out.append(snap)
    return out


def _render_cutouts(
    store: Store,
    settings: Settings,
    root: Path,
    config: "Any",
    latest_ts: int,
    fingerprint: str,
) -> tuple[list[dict[str, Any]], float | None, dict[str, Any]]:
    """Per-source cutouts + their PSF ceilings for the latest integration.

    docs/plan.md "3. Imaging": "per-source cutouts (``image_around_source``)
    around whichever of Sun/Cyg A/Cas A/Tau A is up". Each source above
    :data:`~casm_monitor.figures.imaging_figures.CUTOUT_MIN_ALT_DEG` at the
    latest integration gets one image (5 deg half-width, 51 px, l/m grid) and
    the dirty-beam SNR ceiling for that same geometry.

    Returns ``(cutouts, psf_ceiling_snr, psf_cache)``: ``psf_ceiling_snr`` is
    the ceiling for the HIGHEST source imaged this pass -- the best the
    deployed array configuration can do on the sky the all-sky image was made
    from -- and the cache is written back by the caller.
    """
    imaging = _if()
    meta = {"cal_file": config.cal_file, "n_ant": len(config.antennas)}
    up = [
        m
        for m in imaging.source_marks(latest_ts)
        if m["alt_deg"] > imaging.CUTOUT_MIN_ALT_DEG
    ]
    up.sort(key=lambda m: -m["alt_deg"])

    cache = _read_json(root / "psf_cache.json") or {}
    if cache.get("config_fingerprint") != fingerprint:
        cache = {"config_fingerprint": fingerprint, "ceilings": {}, "cost_s": None, "passes": 0}
    ceilings: dict[str, Any] = dict(cache.get("ceilings") or {})
    cost_s = cache.get("cost_s")
    passes = int(cache.get("passes") or 0) + 1
    # Measure the cost once; only skip passes if it turned out to exceed the
    # budget (and then only 5 passes out of 6).
    compute_psf = (
        cost_s is None
        or float(cost_s) <= PSF_COST_BUDGET_S
        or passes % PSF_PASS_INTERVAL == 0
    )

    cutouts: list[dict[str, Any]] = []
    started = time.time()
    for mark in up:
        source = mark["name"]
        try:
            result = imaging.image_cutout(settings, source, latest_ts, config=config)
        except Exception:  # a cutout is never worth failing the whole pass over
            log.exception("render_figures: cutout for %s failed", source)
            continue
        snr = float(result["snr_info"]["snr"])
        ceiling = ceilings.get(source)
        if compute_psf:
            psf_started = time.time()
            try:
                ceiling = imaging.psf_ceiling(result)
            except Exception:
                log.exception("render_figures: PSF ceiling for %s failed", source)
                ceiling = None
            else:
                cost_s = round(time.time() - psf_started, 3)
                ceilings[source] = ceiling
        pngs = imaging.render_cutout(
            result, source, latest_ts, snr=snr, ceiling_snr=ceiling, meta=meta
        )
        name = safe_name(source, "source")
        for suffix, data in pngs.items():
            _atomic_write(root / f"cutout_{name}@{suffix}.png", data)
        cutouts.append(
            {
                "source": source,
                "alt_deg": mark["alt_deg"],
                "az_deg": mark["az_deg"],
                "file_1x": f"cutout_{name}@1x.png",
                "file_2x": f"cutout_{name}@2x.png",
                "snr": round(snr, 2),
                "ceiling_snr": None if ceiling is None else round(float(ceiling), 2),
            }
        )
    cutout_s = round(time.time() - started, 3)
    store.put_scalar("figures.imaging.cutout_s", cutout_s)
    store.put_scalar("figures.imaging.n_cutouts", len(cutouts))
    if cost_s is not None:
        store.put_scalar("figures.imaging.psf_s", float(cost_s))
    ceiling_snr = None
    for cut in cutouts:  # already sorted by altitude, highest first
        if cut["ceiling_snr"] is not None:
            ceiling_snr = cut["ceiling_snr"]
            break
    cache = {
        "config_fingerprint": fingerprint,
        "ceilings": ceilings,
        "cost_s": cost_s,
        "passes": passes,
        "computed_this_pass": bool(compute_psf),
        "cutout_s": cutout_s,
    }
    return cutouts, ceiling_snr, cache


def _render_imaging(store: Store, settings: Settings) -> dict[str, Any]:
    """All-sky imaging: catch the frame cache up, then rebuild the products."""
    imaging = _if()
    root = ensure_contained(
        Path(settings.store_root) / "figures" / "imaging", settings.store_root
    )
    root.mkdir(parents=True, exist_ok=True)
    frames_dir = _frames_dir(root)
    frames_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    now = started
    # Retention FIRST, whatever happens next: a pass that cannot resolve the
    # deployed cal must still not leave the cache growing for ever.
    n_expired = _expire_frames(frames_dir, now, imaging.FRAME_RETENTION_DAYS)

    try:
        config = imaging.imaging_config(settings)
    except Exception as exc:  # depends on the live ledger/cal/layout files
        log.info("render_figures: imaging unavailable this pass: %s", exc)
        store.put_scalar("figures.imaging.skipped", 1)
        return {"skipped": str(exc), "n_frames_expired": n_expired}

    fingerprint = imaging.config_fingerprint(config)
    n_stale = _expire_stale_frames(frames_dir, fingerprint)
    if n_stale:
        log.info(
            "render_figures: dropped %d imaging frame(s) from a superseded "
            "cal/antenna configuration", n_stale
        )
    window_t0 = now - imaging.WINDOW_HOURS * 3600.0

    old_manifest = _read_json(root / "manifest.json") or {}
    # Where the last pass stopped ASKING, not where it last succeeded: a data
    # gap (every step in the range skipped) must still advance the resume
    # point, or every future pass re-requests the same 64 dead integrations
    # for ever. A manifest written under a DIFFERENT configuration says
    # nothing about this one, so its resume point is discarded with its frames.
    scan_t1 = (
        float(old_manifest.get("scan_t1") or 0.0)
        if old_manifest.get("config_fingerprint") == fingerprint
        else 0.0
    )
    cached = _cached_frame_ts(frames_dir)
    resume = max(scan_t1, float(cached[-1]) + imaging.INTEGRATION_S if cached else 0.0)
    t0 = max(window_t0, resume)
    behind_s = max(0.0, now - t0)
    max_frames = MAX_IMAGING_FRAMES_COLD if behind_s > COLD_START_LAG_S else MAX_IMAGING_FRAMES
    t1 = min(now, t0 + max_frames * imaging.INTEGRATION_S)

    n_new = 0
    if t1 - t0 >= imaging.INTEGRATION_S:
        print(
            f"render_figures: imaging {imaging.utc_iso(t0)} .. {imaging.utc_iso(t1)} "
            f"(cal {config.cal_file}, {len(config.antennas)} antennas, "
            f"{behind_s / 3600.0:.1f} h behind, up to {max_frames} frames)",
            flush=True,
        )
        snaps = imaging.image_window(store, settings, t0, t1, config=config)
        known = set(cached)
        for snap in snaps:
            ts = int(round(float(snap["time_unix"])))
            if ts in known:
                continue
            _write_frame(frames_dir, snap, fingerprint)
            known.add(ts)
            n_new += 1
        cached = sorted(known)
    else:
        t1 = max(t1, scan_t1)

    in_window = [ts for ts in cached if ts >= window_t0]
    result: dict[str, Any] = {
        "n_frames_rendered": n_new,
        "n_frames_cached": len(cached),
        "n_frames_expired": n_expired,
        "n_frames_stale": n_stale,
        "max_frames": max_frames,
        "behind_h": round(behind_s / 3600.0, 2),
        "scan_t0": t0,
        "scan_t1": t1,
        "config_fingerprint": fingerprint,
    }
    if not in_window:
        log.info("render_figures: no imaging frames in the last 24 h yet")
        result["render_s"] = round(time.time() - started, 3)
        result["peak_rss_mb"] = _peak_rss_mb()
        store.put_scalar("figures.imaging.render_s", result["render_s"])
        store.put_scalar("figures.imaging.peak_rss_mb", result["peak_rss_mb"])
        _write_imaging_manifest(
            root, config, fingerprint, None, [], None, window_t0, now, len(cached),
            scan_t1=t1, cutouts=[], psf_ceiling_snr=None,
        )
        return result

    latest_ts = in_window[-1]
    latest_snap = _load_frame(frames_dir, latest_ts)
    if latest_snap is not None:
        pngs = imaging.render_latest(
            latest_snap, {"cal_file": config.cal_file, "n_ant": len(config.antennas)}
        )
        for suffix, data in pngs.items():
            _atomic_write(root / f"latest@{suffix}.png", data)

    strip_ts = _strip_selection(in_window, window_t0, now)
    strip_snaps = _load_frames_one_at_a_time(frames_dir, strip_ts)
    strip_pngs = imaging.render_strip(strip_snaps)
    for suffix, data in strip_pngs.items():
        _atomic_write(root / f"strip24h@{suffix}.png", data)

    movie_name = None
    if imaging.ffmpeg_available():
        # At most MOVIE_MAX_FRAMES TIMESTAMPS, both endpoints included,
        # chosen before a single npz is opened.
        movie_ts = imaging.movie_selection(in_window, imaging.MOVIE_MAX_FRAMES)
        movie_snaps = _load_frames_one_at_a_time(frames_dir, movie_ts)
        try:
            movie_path = imaging.render_movie(movie_snaps, root)
        except Exception:  # a movie is never worth failing the whole pass over
            log.exception("render_figures: imaging movie render failed")
            movie_path = None
        if movie_path is not None:
            movie_name = movie_path.name
    else:
        log.info("render_figures: ffmpeg not on PATH; imaging movie skipped")

    cutouts: list[dict[str, Any]] = []
    psf_ceiling_snr: float | None = None
    if latest_snap is not None:
        cutouts, psf_ceiling_snr, psf_cache = _render_cutouts(
            store, settings, root, config, latest_ts, fingerprint
        )
        tmp = root / f".tmp-{os.getpid()}-psf_cache.json"
        tmp.write_text(json.dumps(psf_cache, indent=2, sort_keys=True))
        os.replace(tmp, root / "psf_cache.json")

    _write_imaging_manifest(
        root, config, fingerprint,
        latest_ts if latest_snap is not None else None,
        strip_ts, movie_name, window_t0, now, len(cached),
        scan_t1=t1, cutouts=cutouts, psf_ceiling_snr=psf_ceiling_snr,
    )

    render_s = round(time.time() - started, 3)
    peak_rss_mb = _peak_rss_mb()
    store.put_scalar("figures.imaging.render_s", render_s)
    store.put_scalar("figures.imaging.peak_rss_mb", peak_rss_mb)
    store.put_scalar("figures.imaging.n_frames_rendered", n_new)
    store.put_scalar("figures.imaging.n_frames_cached", len(cached))
    store.put_scalar("figures.imaging.lag_s", round(now - latest_ts, 1))
    result.update(
        {
            "render_s": render_s,
            "peak_rss_mb": peak_rss_mb,
            "n_strip": len(strip_ts),
            "movie": movie_name,
            "latest_ts": latest_ts,
            "lag_s": round(now - latest_ts, 1),
            "cutouts": [c["source"] for c in cutouts],
            "psf_ceiling_snr": psf_ceiling_snr,
        }
    )
    return result


def _write_imaging_manifest(
    root: Path,
    config: "Any",
    fingerprint: str,
    latest_ts: int | None,
    strip_ts: list[int],
    movie_name: str | None,
    t0: float,
    t1: float,
    n_cached: int,
    scan_t1: float | None = None,
    cutouts: list[dict[str, Any]] | None = None,
    psf_ceiling_snr: float | None = None,
) -> None:
    """The manifest docs/api-imaging.md specifies, exactly.

    ``latest.lag_s`` is now minus the latest imaged integration, so the page
    can say "latest image is 3.2 h behind" without doing arithmetic on two
    timestamps. ``psf_ceiling_snr`` is the dirty-beam ceiling of the highest
    source imaged this pass (``cutouts[].ceiling_snr`` carries the per-source
    ones), null when no source was up or the PSF replay failed.
    ``config_fingerprint`` identifies the deployed cal + antenna set + imaging
    parameters every cached frame in these products was made with; a frame
    with any other fingerprint is expired rather than shown.
    ``scan_t1``/``n_frames_cached`` are the monitor's own bookkeeping, not part
    of the contract -- ``scan_t1`` is where the next pass resumes imaging.
    """
    imaging = _if()
    manifest: dict[str, Any] = {
        "rendered_utc": imaging.utc_iso(time.time()),
        "cal_file": config.cal_file,
        "antennas": list(config.antennas),
        "config_fingerprint": fingerprint,
        "latest": None
        if latest_ts is None
        else {
            "ts": imaging.utc_iso(latest_ts),
            "ts_unix": int(latest_ts),
            "lag_s": round(float(t1) - float(latest_ts), 1),
            "file_1x": "latest@1x.png",
            "file_2x": "latest@2x.png",
        },
        "strip": {
            "t0": imaging.utc_iso(t0),
            "t1": imaging.utc_iso(t1),
            "n": len(strip_ts),
            "file_1x": "strip24h@1x.png",
            "file_2x": "strip24h@2x.png",
        },
        "movie": {"file": movie_name, "fps": imaging.MOVIE_FPS},
        "sources": imaging.source_marks(time.time()),
        "cutouts": list(cutouts or []),
        "psf_ceiling_snr": psf_ceiling_snr,
        "n_frames_cached": n_cached,
        "scan_t1": float(t1 if scan_t1 is None else scan_t1),
    }
    path = root / "manifest.json"
    tmp = root / f".tmp-{os.getpid()}-manifest.json"
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    os.replace(tmp, path)


# -- Candidates tab ---------------------------------------------------------
# The T2 funnel chart. It lives here, and not behind
# ``GET /api/cands/stats/plot.png``, because a GET route must never render
# (2026-09-09 review, finding 1: the old route wrote into t3's shared temp
# dir, outside the store root, from a read-only API). One PNG per
# ``statsplot.WINDOW_PRESETS`` window, so switching the window on the page
# cannot serve the wrong chart; the 24 h default is the contract's
# ``funnel@1x.png``.
def _render_cands(store: Store, settings: Settings) -> dict[str, Any]:
    """Render the T2 funnel PNGs into ``store_root/figures/cands/``."""
    from casm_t3.web import statsplot

    from ..web.cands import funnel_dir, funnel_filename

    root = funnel_dir(settings)
    root.mkdir(parents=True, exist_ok=True)
    started = time.time()
    files: dict[str, str] = {}
    failed: dict[str, str] = {}
    for hours, _label in statsplot.WINDOW_PRESETS:
        name = funnel_filename(hours)
        tmp = root / f".tmp-{os.getpid()}-{name}"
        try:
            # statsplot.render writes its own temp file next to the target and
            # replaces it, so it is already atomic; rendering into our own tmp
            # name first keeps a failed render from truncating the served file.
            statsplot.render(str(settings.t2_db), tmp, hours=hours)
            _atomic_write(root / name, tmp.read_bytes())
            files[str(hours)] = name
        except Exception as exc:
            log.exception("render_figures: funnel chart for %d h failed", hours)
            failed[str(hours)] = f"{type(exc).__name__}: {exc}"
        finally:
            tmp.unlink(missing_ok=True)
    render_s = round(time.time() - started, 3)
    manifest = {
        "rendered_utc": _if().utc_iso(time.time()),
        "db": str(settings.t2_db),
        "hours": [h for h, _ in statsplot.WINDOW_PRESETS],
        "files": files,
        "failed": failed,
        "render_s": render_s,
    }
    tmp = root / f".tmp-{os.getpid()}-manifest.json"
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    os.replace(tmp, root / "manifest.json")
    store.put_scalar("figures.cands.render_s", render_s)
    store.put_scalar("figures.cands.n_rendered", len(files))
    store.put_scalar("figures.cands.n_failed", len(failed))
    return {"render_s": render_s, "files": files, "failed": failed}



# -- job entry point --------------------------------------------------------
def run(params: dict[str, Any]) -> dict[str, Any]:
    """Job entry point (runs in the job subprocess, see jobs/run_job.py)."""
    settings = load_settings(params.get("config"))
    targets = [str(t) for t in (params.get("targets") or list(TARGETS))]
    unknown = [t for t in targets if t not in TARGETS]
    if unknown:
        raise ValueError(f"unknown render_figures target(s) {unknown}; known: {TARGETS}")
    reason = str(params.get("reason") or "scheduled")
    store = Store(settings.db_path, store_root=settings.store_root)
    result: dict[str, Any] = {"reason": reason, "targets": targets}
    try:
        started = time.time()
        if "vis" in targets:
            print("render_figures: rendering vis", flush=True)
            result["vis"] = _render_vis(store, settings)
            print(f"render_figures: vis done: {result['vis']}", flush=True)
        if "snaps" in targets:
            print("render_figures: rendering snaps", flush=True)
            result["snaps"] = _render_snaps(store, settings)
            print(f"render_figures: snaps done: {result['snaps']}", flush=True)
        if "imaging" in targets:
            print("render_figures: rendering imaging", flush=True)
            result["imaging"] = _render_imaging(store, settings)
            print(f"render_figures: imaging done: {result['imaging']}", flush=True)
        if "cands" in targets:
            print("render_figures: rendering cands", flush=True)
            result["cands"] = _render_cands(store, settings)
            print(f"render_figures: cands done: {result['cands']}", flush=True)
        result["elapsed_s"] = round(time.time() - started, 3)
        store.add_event(
            "render_figures",
            severity="info",
            subject=f"targets={targets}",
            detail={"reason": reason, "targets": targets, "elapsed_s": result["elapsed_s"]},
        )
        return result
    finally:
        store.close()
