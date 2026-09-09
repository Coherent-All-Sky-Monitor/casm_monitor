"""The ``render_figures`` job: renders the Vis and SNAPs tab PNGs.

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

TARGETS = ("vis", "snaps")


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
