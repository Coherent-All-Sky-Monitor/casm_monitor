"""``FigureCollector`` (M2 figures): server-rendered Visibilities PNGs.

Every ``cadences.figures`` seconds (default 1800 s) it renders every
``(kind, set, ref)`` combination :mod:`casm_monitor.figures.vis_figures`
knows about -- ``matrix_<quantity>``/``spectra_<quantity>`` for the five
quantities plus ``autos``, for input set ``live``/``wired``, for reference
``raw``/``sun`` always and ``cal`` when the deployed cal resolves this pass --
into ``store_root/figures/vis/<set>/<ref>/<kind>@{1x,2x}.png`` (atomic tmp +
rename) plus one ``manifest.json`` per ``<set>/<ref>`` directory.

The 24 h window is set-only (the reference is applied afterwards), so it is
loaded ONCE per set and reused across every ``ref``/``kind`` in that set,
rather than re-reading the shards ``2 (sets) x 3 (refs) x 11 (kinds)`` times.
That window load also gives a cheap skip test: when its newest timestamp and
sample count match the last manifest written for this set, nothing in the
cache has changed since the last render and the whole set is skipped (an
event-free, scalar-recorded no-op, not a failure).

Rendering itself runs in a thread pool (default 16 workers, matching the
plan's "16 threads" budget): :mod:`casm_monitor.figures.vis_figures` builds
each figure with ``Figure``/``FigureCanvasAgg`` rather than ``pyplot``
precisely so this is safe (no shared pyplot figure manager across threads).
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from ..figures import vis_figures as vf

from ..store.shards import ensure_contained, safe_name
from ..web.vis import load_deployed_cal
from .base import Collector, CollectorContext

log = logging.getLogger("casm_monitor.collect.figures")

MAX_WORKERS = 16
BASE_REFS = ("raw", "sun")


def _vf():
    """Lazy import: vis_figures imports web.vis which imports collectors."""
    from ..figures import vis_figures
    return vis_figures


class FigureCollector(Collector):
    """Renders the Visibilities tab's matplotlib figures."""

    name = "figures"
    default_cadence_s = 1800.0
    # Generous but bounded: the plan's budget is "the whole set under 5 min on
    # 16 threads"; the runner isolates one slow pass without blocking others.
    timeout_s = 280.0

    def collect(self, ctx: CollectorContext) -> None:
        settings = ctx.settings
        root = ensure_contained(
            Path(settings.store_root) / "figures" / "vis", settings.store_root
        )
        refs = list(BASE_REFS)
        try:
            load_deployed_cal(settings)
            refs.append("cal")
        except Exception as exc:  # depends on the live ledger/cal file
            log.info("figures: cal reference unavailable this pass: %s", exc)

        started = time.time()
        n_rendered = 0
        n_skipped = 0
        n_failed = 0
        for set_name in _vf().SETS:
            try:
                window = _vf().load_window(ctx.store, settings, set_name)
            except _vf().NoData as exc:
                log.info("figures: no data for set=%s: %s", set_name, exc)
                continue
            watermark = self._read_manifest(root, set_name, refs[0])
            if watermark is not None and self._unchanged(watermark, window):
                ctx.scalar("figures.vis.skipped", 1, tags={"set": set_name})
                n_skipped += len(_vf().KINDS) * len(refs)
                continue
            rendered, failed = self._render_set(ctx, root, set_name, refs, window)
            n_rendered += rendered
            n_failed += failed
        total_s = time.time() - started
        ctx.scalar("figures.vis.render_s", round(total_s, 3))
        ctx.scalar("figures.vis.n_rendered", n_rendered)
        ctx.scalar("figures.vis.n_skipped", n_skipped)
        ctx.scalar("figures.vis.n_failed", n_failed)

    # -- one set, every (kind, ref) ------------------------------------
    def _render_set(
        self,
        ctx: CollectorContext,
        root: Path,
        set_name: str,
        refs: list[str],
        window: "vf.WindowData",
    ) -> tuple[int, int]:
        jobs = [(kind, ref) for ref in refs for kind in _vf().KINDS]
        by_ref: dict[str, list[dict[str, Any]]] = {ref: [] for ref in refs}
        failed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(self._render_one, ctx, set_name, ref, kind, root, window): (kind, ref)
                for kind, ref in jobs
            }
            for fut in concurrent.futures.as_completed(futures):
                kind, ref = futures[fut]
                try:
                    entry = fut.result()
                except Exception as exc:  # never let one figure kill the pass
                    failed += 1
                    log.exception(
                        "figures: render failed for %s/%s/%s", set_name, ref, kind
                    )
                    ctx.event(
                        "figures_render_failed",
                        severity="warn",
                        subject=f"{set_name}/{ref}/{kind}",
                        detail={"error": str(exc)},
                    )
                    continue
                by_ref[ref].append(entry)
        rendered = sum(len(v) for v in by_ref.values())
        for ref, entries in by_ref.items():
            if entries:
                self._write_manifest(root, set_name, ref, entries)
        return rendered, failed

    def _render_one(
        self,
        ctx: CollectorContext,
        set_name: str,
        ref: str,
        kind: str,
        root: Path,
        window: "vf.WindowData",
    ) -> dict[str, Any]:
        started = time.time()
        pngs, info = _vf().render_kind(ctx.store, ctx.settings, kind, set_name, ref, window=window)
        render_s = time.time() - started
        ctx.scalar(
            "figures.vis.render_s",
            round(render_s, 3),
            tags={"kind": kind, "set": set_name, "ref": ref},
        )
        outdir = ensure_contained(root / safe_name(set_name) / safe_name(ref), root)
        outdir.mkdir(parents=True, exist_ok=True)
        for suffix, data in pngs.items():
            self._atomic_write(outdir / f"{kind}@{suffix}.png", data)
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

    # -- atomic writes ---------------------------------------------------
    @staticmethod
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

    def _write_manifest(
        self, root: Path, set_name: str, ref: str, entries: list[dict[str, Any]]
    ) -> None:
        outdir = ensure_contained(root / safe_name(set_name) / safe_name(ref), root)
        outdir.mkdir(parents=True, exist_ok=True)
        rendered_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        manifest = {
            "rendered_utc": rendered_utc,
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

    # -- skip-when-unchanged ----------------------------------------------
    @staticmethod
    def _read_manifest(root: Path, set_name: str, ref: str) -> dict[str, Any] | None:
        path = root / set_name / ref / "manifest.json"
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _unchanged(manifest: dict[str, Any], window: "vf.WindowData") -> bool:
        """True when the newest cached integration matches the last render.

        Compares against ``window.times[-1]`` (the newest CACHED sample), not
        ``window.t1`` (the query boundary, always ``~now``): the latter would
        never repeat between passes and the skip would never fire.
        """
        if not len(window.times):
            return False
        return (
            manifest.get("t1") == float(window.times[-1])
            and manifest.get("n_integrations") == len(window.times)
        )


__all__ = ["FigureCollector"]
