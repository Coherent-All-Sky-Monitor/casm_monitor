"""The ``cal_build`` job: one canonical cal + weights product.

The body is a call to ``bf_weights_generator.make_cal_and_weights.run`` with a
``RecipeParams`` built from a VALIDATED subset of its knobs. That driver is the
only way a cal or weights product may be built (binding rule, casm-wiki
``weights-and-deploy.md``): nothing here solves, grids or quantizes anything of
its own, and the parameters this module refuses are refused before the driver
sees them, not silently corrected.

Fixed by this job, not exposed as knobs:

* ``grid_mode="exact"`` and ``n_beams=512`` — exact grid only, exactly 512 beams
  (plan.md hard rules). ``FrequencyConfig.layout_64ant()`` and
  ``freq_order="descending"`` are the driver's own fixed policy and are not
  restated here.
* ``diagnostics``/``notebook``/``execute_notebook`` — every product ships with
  the executed diagnostics notebook (weights-and-deploy.md).
* ``layout_csv`` — the ``current`` symlink, snapshotted BYTE FOR BYTE into the
  build directory with its sha256 before the run. ``deploy_upload`` compares
  that sha against the layout in force at upload time, so a layout repoint
  between build and upload cannot pass unnoticed (plan.md M3
  layout-provenance check), and the snapshot survives a later repoint.
* ``out_dir`` — ``store_root/cal_builds/<tag>/``, refused if it resolves
  outside the store root. Nothing is written anywhere else.

The driver prints its whole log to stdout, which the worker has already
redirected into the job log; it is tee'd into ``<out_dir>/build.log`` as well so
the build keeps its own log after the job row is pruned.
"""

from __future__ import annotations

import json
import os
import re
import resource
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from ..cal_defaults import deployed_product, layout_info
from ..config import Settings, load_settings
from ..store import Store
from ..store.shards import ensure_contained, safe_name
from ..util import iso

#: Fixed by the plan's hard rules; a request that says anything else is refused.
GRID_MODE = "exact"
N_BEAMS = 512

#: Bounds on the requested windows. A solve window is an hour by convention
#: (the deployed products); anything beyond six hours is a typo, not a request.
MIN_WINDOW_S = 60.0
MAX_WINDOW_S = 6 * 3600.0

TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")

#: Report keys copied into summary.json's ``numbers`` block verbatim.
REPORT_NUMBER_KEYS = (
    "cal",
    "cal_subbands",
    "pointings",
    "pointing_fit",
    "int8",
    "grid",
    "gain_delay_fits",
    "rank1_medians",
    "svd_vs_freq",
    "static_ab",
    "beam_check",
    "cal_diff_band_avg_coherence",
    "nearest_beams",
    "diagnostic_failures",
    "diagnostics_error",
)


class ParamError(ValueError):
    """A build request that must be refused (400) rather than run."""


# ---------------------------------------------------------------------------
# Parameter validation (shared by the router and the job body)
# ---------------------------------------------------------------------------

def _parse_utc(value: Any, field: str) -> tuple[float, str]:
    """Accept ISO-8601 (``...T...Z``, the API's wire format) or the driver's
    ``"YYYY-MM-DD HH:MM:SS"``; return ``(unix seconds, driver spelling)``.

    Normalising here is what lets the tab speak one format while
    ``RecipeParams`` gets the one the driver parses.
    """
    from astropy.time import Time

    text = str(value).strip().replace("T", " ")
    if text.endswith("Z"):
        text = text[:-1].strip()
    try:
        t = Time(text, scale="utc")
        return float(t.unix), str(t.iso)[:19]
    except Exception as exc:  # noqa: BLE001 - astropy raises several types
        raise ParamError(f"{field} {value!r} is not a UTC timestamp: {exc}") from exc


def _window(value: Any, field: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ParamError(f"{field} must be [start, end] in UTC")
    t0, s0 = _parse_utc(value[0], f"{field}[0]")
    t1, s1 = _parse_utc(value[1], f"{field}[1]")
    if t1 <= t0:
        raise ParamError(f"{field} ends at or before it starts")
    if t1 - t0 < MIN_WINDOW_S:
        raise ParamError(f"{field} is shorter than {MIN_WINDOW_S:.0f} s")
    if t1 - t0 > MAX_WINDOW_S:
        raise ParamError(f"{field} is longer than {MAX_WINDOW_S / 3600:.0f} h")
    return [s0, s1]


def validate(params: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Normalise + refuse a build request. Raises :class:`ParamError`.

    Called by the router (so a bad request is a 400 before a job exists) and
    again inside the job (so a hand-inserted job row gets the same treatment).
    """
    p = dict(params or {})

    source = str(p.get("source") or "").strip().lower()
    enabled = [s.lower() for s in settings.cal_sources_enabled]
    if source not in enabled:
        raise ParamError(
            f"source {source or '(missing)'!r} is not enabled; the array solves on "
            f"{', '.join(enabled)} today (config cal.sources_enabled)"
        )

    tag = str(p.get("tag") or "").strip()
    if not TAG_RE.fullmatch(tag):
        raise ParamError("tag must match [A-Za-z0-9][A-Za-z0-9_.-]*")
    safe_name(tag, "build tag")

    source_window = _window(p.get("source_window"), "source_window")
    raw_static = p.get("static_window")
    static = None if raw_static in (None, [], ()) else _window(raw_static, "static_window")

    layout = layout_info(settings.layout_csv)
    allowed = set(layout["antennas"])
    antennas = p.get("antennas")
    if not isinstance(antennas, (list, tuple)) or not antennas:
        raise ParamError("antennas must be a non-empty list of antenna ids")
    try:
        ants = sorted({int(a) for a in antennas})
    except (TypeError, ValueError) as exc:
        raise ParamError(f"antennas must be integers: {exc}") from exc
    outside = [a for a in ants if a not in allowed]
    if outside:
        # The weights stage intersects the antenna list with this same column
        # and produces a near-empty file that still PASSES driver verification
        # (casm-wiki incidents.md 2026-08-31), so the mismatch is refused here.
        raise ParamError(
            f"antennas {outside} are not include_in_beamforming=1 in "
            f"{layout['path']}; the weights stage would silently drop them"
        )
    ref_ant = p.get("ref_ant")
    try:
        ref_ant = int(ref_ant)
    except (TypeError, ValueError) as exc:
        raise ParamError(f"ref_ant must be an integer: {exc}") from exc
    if ref_ant not in ants:
        raise ParamError(f"ref_ant {ref_ant} is not in the antenna set {ants}")

    grid_mode = str(p.get("grid_mode") or GRID_MODE)
    if grid_mode != GRID_MODE:
        raise ParamError(f"grid_mode is fixed at {GRID_MODE!r} (plan.md hard rules)")
    n_beams = int(p.get("n_beams") or N_BEAMS)
    if n_beams != N_BEAMS:
        raise ParamError(f"n_beams is fixed at {N_BEAMS} (the beamformer requires exactly that)")
    for flag in ("diagnostics", "notebook", "execute_notebook"):
        if p.get(flag, True) is not True:
            raise ParamError(
                f"{flag} cannot be turned off: every product ships with the executed "
                f"diagnostics notebook (casm-wiki weights-and-deploy.md)"
            )

    prev_cal = p.get("prev_cal_path")
    if prev_cal is None:
        prev_cal = deployed_product(settings).get("cal_file")
    if prev_cal is not None:
        prev_cal = str(prev_cal)
        if not Path(prev_cal).is_file():
            raise ParamError(f"prev_cal_path {prev_cal} does not exist")

    return {
        "source": source,
        "tag": tag,
        "source_window": source_window,
        "static_window": static,
        "antennas": ants,
        "ref_ant": ref_ant,
        "grid_mode": GRID_MODE,
        "n_beams": N_BEAMS,
        "diagnostics": True,
        "notebook": True,
        "execute_notebook": True,
        "prev_cal_path": prev_cal,
        "layout_csv": str(settings.layout_csv),
    }


def build_dir(settings: Settings, tag: str) -> Path:
    """``store_root/cal_builds/<tag>``, refused if it escapes the store root."""
    root = ensure_contained(settings.cal_builds_root, Path(settings.store_root))
    return ensure_contained(root / safe_name(tag, "build tag"), root)


def summary_path(settings: Settings, tag: str) -> Path:
    return build_dir(settings, tag) / "summary.json"


# ---------------------------------------------------------------------------
# Job body
# ---------------------------------------------------------------------------

class _Tee:
    """stdout that also lands in the build's own log file."""

    def __init__(self, stream: Any, path: Path) -> None:
        self._stream = stream
        self._fh = path.open("a", buffering=1)

    def write(self, text: str) -> int:
        self._stream.write(text)
        self._fh.write(text)
        return len(text)

    def flush(self) -> None:
        self._stream.flush()
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def __getattr__(self, name: str) -> Any:  # isatty(), fileno(), ...
        return getattr(self._stream, name)


def _peak_rss_mb() -> float:
    """Whole-process RSS high-water mark, MB (``ru_maxrss`` is KB on Linux)."""
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)


def _figs(out_dir: Path) -> list[str]:
    """Figure paths relative to ``out_dir/figs`` (fringe/ subdirectory included)."""
    figs_dir = out_dir / "figs"
    if not figs_dir.is_dir():
        return []
    return sorted(
        str(p.relative_to(figs_dir)) for p in figs_dir.rglob("*.png") if p.is_file()
    )


def _numbers(report: dict[str, Any]) -> dict[str, Any]:
    return {k: report[k] for k in REPORT_NUMBER_KEYS if k in report}


def _first(paths: Iterable[Path]) -> str | None:
    for p in sorted(paths):
        return str(p)
    return None


def run(params: dict[str, Any]) -> dict[str, Any]:
    """Job entry point (runs in the job subprocess, see jobs/run_job.py)."""
    settings = load_settings(params.get("config"))
    p = validate(params, settings)
    tag = p["tag"]
    out_dir = build_dir(settings, tag)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Layout provenance FIRST: the snapshot and its sha are what the upload
    # gate compares against, so they are written before anything can fail.
    layout = layout_info(p["layout_csv"])
    snapshot = out_dir / "layout_snapshot.csv"
    if Path(layout["resolved"]).is_file():
        shutil.copyfile(layout["resolved"], snapshot)
    layout["snapshot"] = str(snapshot)

    deployed = deployed_product(settings)
    started = time.time()
    log_path = out_dir / "build.log"
    tee = _Tee(sys.stdout, log_path)
    old_stdout = sys.stdout
    sys.stdout = tee  # type: ignore[assignment]
    try:
        from bf_weights_generator.make_cal_and_weights import RecipeParams
        from bf_weights_generator.make_cal_and_weights import run as run_recipe

        date = p["source_window"][0][:10]
        recipe = RecipeParams(
            out_dir=str(out_dir),
            tag=tag,
            cal_source=p["source"],
            source_window=tuple(p["source_window"]),
            static_window=None if p["static_window"] is None else tuple(p["static_window"]),
            antennas=tuple(p["antennas"]),
            ref_ant=p["ref_ant"],
            layout_csv=p["layout_csv"],
            grid_mode=GRID_MODE,
            n_beams=N_BEAMS,
            prev_cal_path=p["prev_cal_path"],
            diagnostics=True,
            notebook=True,
            execute_notebook=True,
            nearest_sources=(p["source"],),
            nearest_date=date,
            transit_date=date,
        )
        print(
            f"[cal_build] tag {tag}\n"
            f"  out_dir {out_dir}\n"
            f"  layout {layout['path']} -> {layout['resolved']} sha256 {layout['sha256']}\n"
            f"  {len(p['antennas'])} antennas {p['antennas']} ref {p['ref_ant']}\n"
            f"  source {p['source']} {p['source_window'][0]} -> {p['source_window'][1]} UTC\n"
            f"  static {p['static_window']}\n"
            f"  prev_cal {p['prev_cal_path']}",
            flush=True,
        )
        out = run_recipe(recipe)
    finally:
        sys.stdout = old_stdout
        tee.flush()
        tee.close()

    elapsed = time.time() - started
    peak_rss_mb = _peak_rss_mb()
    report = dict(out.get("report") or {})
    cal_file = out.get("cal_file")
    weights_file = out.get("weights_file")
    # The canonical driver builds the CB product only; the IB mask is a
    # companion file (gen_ib_from_cb.py, docs/ib_weights.md). If a matching one
    # ever appears in the build directory it is picked up, otherwise the
    # deployed mask is what deploy_stage pairs with (recorded below).
    ib_file = _first(out_dir.glob("ib_*.h5"))

    summary: dict[str, Any] = {
        "tag": tag,
        "kind": "cal_build",
        "created_utc": iso(started),
        "finished_utc": iso(time.time()),
        "wall_s": round(elapsed, 1),
        "peak_rss_mb": peak_rss_mb,
        "params": p,
        "source": p["source"],
        "source_window": p["source_window"],
        "static_window": p["static_window"],
        "antennas": p["antennas"],
        "n_ant": len(p["antennas"]),
        "ref_ant": p["ref_ant"],
        "grid_mode": GRID_MODE,
        "n_beams": N_BEAMS,
        "layout": layout,
        "prev_cal_path": p["prev_cal_path"],
        "deployed_at_build": deployed,
        "paths": {
            "out_dir": str(out_dir),
            "cal_h5": cal_file,
            "weights_h5": weights_file,
            "ib_h5": ib_file,
            "report_json": out.get("report_json"),
            "notebook": out.get("notebook"),
            "build_log": str(log_path),
            "layout_snapshot": str(snapshot),
        },
        "figs": _figs(out_dir),
        "numbers": _numbers(report),
        "rank1_median": (report.get("cal") or {}).get("rank1_median"),
        "notebook_executed": bool(out.get("notebook")),
        "has_weights": bool(weights_file and Path(weights_file).is_file()),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, default=str, indent=1))

    store = Store(settings.db_path, store_root=settings.store_root)
    try:
        store.add_event(
            "cal_build_done",
            severity="info",
            subject=tag,
            detail={
                "tag": tag,
                "n_ant": summary["n_ant"],
                "rank1_median": summary["rank1_median"],
                "weights_h5": weights_file,
                "wall_s": summary["wall_s"],
                "peak_rss_mb": peak_rss_mb,
            },
        )
        store.put_scalar("cal.last_build_tag", tag)
        if summary["rank1_median"] is not None:
            store.put_scalar("cal.last_rank1_median", float(summary["rank1_median"]))
        store.put_scalar("cal.last_build_wall_s", round(elapsed, 1))
        store.put_scalar("cal.last_build_peak_rss_mb", peak_rss_mb)
    finally:
        store.close()

    return {
        "tag": tag,
        "out_dir": str(out_dir),
        "summary": str(out_dir / "summary.json"),
        "cal_h5": cal_file,
        "weights_h5": weights_file,
        "rank1_median": summary["rank1_median"],
        "wall_s": summary["wall_s"],
        "peak_rss_mb": peak_rss_mb,
        "n_figs": len(summary["figs"]),
        "notebook": out.get("notebook"),
    }


def load_summary(settings: Settings, tag: str) -> dict[str, Any] | None:
    path = summary_path(settings, tag)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def list_builds(settings: Settings) -> list[str]:
    root = settings.cal_builds_root
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


__all__ = [
    "ParamError",
    "build_dir",
    "list_builds",
    "load_summary",
    "run",
    "summary_path",
    "validate",
]


if __name__ == "__main__":  # pragma: no cover - manual smoke run
    print(json.dumps(run(json.loads(os.environ.get("CAL_BUILD_PARAMS", "{}"))), indent=1))
