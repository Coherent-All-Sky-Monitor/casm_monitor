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
  build directory before the run; the sha256 that is recorded is THE COPY's,
  and the copy is what ``RecipeParams`` solves against (hashing the live file
  and copying afterwards left a window in which the two disagreed, 2026-09-09
  security review, finding 5). ``deploy_upload`` compares that sha against the
  layout in force at upload time AND re-hashes the copy, so neither a layout
  repoint between build and upload nor an edited snapshot passes unnoticed
  (plan.md M3 layout-provenance check).
* ``out_dir`` — ``store_root/cal_builds/<tag>/``, refused if it resolves
  outside the store root. Nothing is written anywhere else.

The driver prints its whole log to stdout, which the worker has already
redirected into the job log; it is tee'd into ``<out_dir>/build.log`` as well so
the build keeps its own log after the job row is pruned.

After the driver produces the CB weights, this job ALSO generates the paired
IB (incoherent-beam) mask for exactly this build's antenna set, calling
``settings.cal_ib_generator_script``'s ``main()`` (``gen_ib_from_cb.py``, the
only IB-mask generator this service is allowed to run — never hand-rolled
here, casm-wiki ``weights-and-deploy.md`` step 6). It is executed in-process,
so both its path and its sha256 are pinned (``cal.ib_generator_script`` and
``cal.ib_generator_sha256``) and checked when the request is validated as well
as when it is loaded; an unpinned or edited script refuses the build. The mask
is saved as
``ib_<tag>_<n_ant>ant.h5`` in the build directory and recorded at
``summary["paths"]["ib_h5"]``; ``deploy_stage`` stages THAT file, not the
deployed one, so a new antenna set never gets staged with a stale IB.
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
from typing import Any

from ..cal_defaults import deployed_cb_antennas, deployed_product, layout_info, sha256_file
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
#: How far back a solve window may reach. Data older than this is not on the
#: nvme4 spool any more, and a window that far back is a typo or a replayed
#: request, not a solve somebody means to deploy.
MAX_WINDOW_AGE_S = 30 * 86400.0

#: Tag charset and length, the same string that becomes a directory name under
#: ``cal_builds_root`` and a filename component of every product.
TAG_RE = re.compile(r"[A-Za-z0-9_.-]{3,64}")

#: The ONE calibrator this service may solve on. ``cal.sources_enabled`` can
#: only ever narrow this, never widen it: a config edit must not be able to
#: turn on a source the array cannot solve on (2026-09-09 security review,
#: finding 8). Adding one is a code change plus a config change, on purpose.
ALLOWED_SOURCES = ("sun",)

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


#: ``gen_ib_from_cb.py``'s ``bf_scale_factor`` argument only affects the
#: metadata attrs it writes (the mask dataset is a pure 0/1 binary and does
#: not depend on it); 127 matches the CB int8 ``scale_factor`` and the
#: deployed 2026-09-04 pairing (``cb_int8_scale=127``), so the recorded
#: provenance is not stale from the old bf_scale_factor=64 era
#: (weights-and-deploy.md deploy step 3).
IB_BF_SCALE_FACTOR = 127


class ParamError(ValueError):
    """A build request that must be refused (400) rather than run."""


class IBGenerationError(RuntimeError):
    """The IB companion mask could not be built for a finished CB weights file."""


def ib_generator_refusal(
    script_path: str | Path, configured_path: str | Path, expected_sha256: str | None
) -> str | None:
    """Why this IB generator must not be executed, or None.

    The generator lives in a scratch directory anyone can edit and cal_build
    execs it IN-PROCESS, so two things are checked before it is loaded: the
    path is exactly the configured one, and its bytes hash to the value pinned
    in the config (``cal.ib_generator_sha256``). An absent pin is a refusal,
    not a bypass (2026-09-09 security review, finding 8).
    """
    path = Path(script_path)
    configured = Path(configured_path)
    if path.is_symlink() or configured.is_symlink():
        return f"IB mask generator {path} is a symlink; refusing to execute it"
    if str(path) != str(configured):
        return (
            f"IB mask generator {path} is not the configured "
            f"cal.ib_generator_script {configured}"
        )
    if not path.is_file():
        return (
            f"IB mask generator {path} does not exist; cal.ib_generator_script "
            f"must point at gen_ib_from_cb.py (casm-wiki weights-and-deploy.md "
            f"step 6 has its current location)"
        )
    if not expected_sha256:
        return (
            f"no cal.ib_generator_sha256 pinned in the config for {path}; refusing to "
            f"execute an unpinned generator (pin the reviewed script's sha256)"
        )
    actual = sha256_file(path)
    if actual != str(expected_sha256).strip():
        return (
            f"IB mask generator {path} sha256 {actual} does not match the pinned "
            f"cal.ib_generator_sha256 {expected_sha256}; the script changed — review "
            f"it and re-pin before building"
        )
    return None


def _load_ib_generator(
    script_path: str | Path,
    *,
    configured_path: str | Path | None = None,
    expected_sha256: str | None = None,
) -> Any:
    """Load ``gen_ib_from_cb.py`` (or whatever script is configured) as a
    module, by path: it lives outside any installed package (a scratch
    script, casm-wiki ``weights-and-deploy.md`` step 6), so it is imported by
    file location rather than name — after :func:`ib_generator_refusal` has
    approved that exact path and those exact bytes.
    """
    import importlib.util

    path = Path(script_path)
    refusal = ib_generator_refusal(
        path, configured_path if configured_path is not None else path, expected_sha256
    )
    if refusal is not None:
        raise IBGenerationError(refusal)
    spec = importlib.util.spec_from_file_location("_casm_monitor_gen_ib_from_cb", path)
    if spec is None or spec.loader is None:
        raise IBGenerationError(f"could not load the IB mask generator from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def generate_ib_mask(
    cb_h5: str | Path,
    out_dir: str | Path,
    tag: str,
    n_ant: int,
    script_path: str | Path,
    expected_sha256: str | None = None,
    configured_path: str | Path | None = None,
) -> Path:
    """Build this build's OWN IB companion from its OWN CB weights file.

    Calls the generator's ``main(cb_h5, out_h5, bf_scale_factor)`` directly
    (never hand-rolls the mask format/attrs): the function derives the active
    antenna set from the CB file's ``array_config/active_mask``, so the mask
    is guaranteed matched to this build, not the deployed one.
    """
    module = _load_ib_generator(
        script_path,
        configured_path=configured_path if configured_path is not None else script_path,
        expected_sha256=expected_sha256,
    )
    out_path = Path(out_dir) / f"ib_{tag}_{int(n_ant)}ant.h5"
    if not hasattr(module, "main"):
        raise IBGenerationError(f"{script_path} has no main(cb_h5, out_h5, bf_scale) function")
    module.main(str(cb_h5), str(out_path), IB_BF_SCALE_FACTOR)
    if not out_path.is_file():
        raise IBGenerationError(f"{script_path} did not write {out_path}")
    return out_path


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


def _window(value: Any, field: str, *, now: float | None = None) -> tuple[list[str], float, float]:
    """``([driver spelling, driver spelling], t0, t1)``, or :class:`ParamError`.

    Besides the length bounds, the window must lie in the observable past: not
    in the future (there are no visibilities for it) and not further back than
    :data:`MAX_WINDOW_AGE_S` (2026-09-09 security review, finding 8).
    """
    t_now = time.time() if now is None else now
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
    if t1 > t_now:
        raise ParamError(f"{field} ends in the future ({s1} UTC); there is no data for it yet")
    if t0 < t_now - MAX_WINDOW_AGE_S:
        raise ParamError(
            f"{field} starts {(t_now - t0) / 86400.0:.1f} days ago, beyond the "
            f"{MAX_WINDOW_AGE_S / 86400.0:.0f}-day limit"
        )
    return [s0, s1], t0, t1


def validate(params: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Normalise + refuse a build request. Raises :class:`ParamError`.

    Called by the router (so a bad request is a 400 before a job exists) and
    again inside the job (so a hand-inserted job row gets the same treatment).
    """
    p = dict(params or {})

    source = str(p.get("source") or "").strip().lower()
    enabled = [s.lower() for s in settings.cal_sources_enabled]
    # BOTH gates, not either: the config can narrow the list, never widen it
    # past ALLOWED_SOURCES.
    if source not in ALLOWED_SOURCES or source not in enabled:
        raise ParamError(
            f"source {source or '(missing)'!r} is not enabled; this service solves on "
            f"{', '.join(ALLOWED_SOURCES)} only (config cal.sources_enabled currently "
            f"lists {', '.join(enabled) or 'nothing'})"
        )

    tag = str(p.get("tag") or "").strip()
    if not TAG_RE.fullmatch(tag):
        raise ParamError("tag must match [A-Za-z0-9_.-]{3,64}")
    safe_name(tag, "build tag")

    source_window, src_t0, src_t1 = _window(p.get("source_window"), "source_window")
    raw_static = p.get("static_window")
    static: list[str] | None = None
    if raw_static not in (None, [], ()):
        static, st_t0, st_t1 = _window(raw_static, "static_window")
        # An off-source template taken DURING the solve window is not a static
        # template at all: it carries the source (weights-verification.md's
        # static-amplitude trap).
        if src_t0 < st_t1 and st_t0 < src_t1:
            raise ParamError(
                f"source_window {source_window} and static_window {static} overlap; "
                f"the static template must come from an off-source window"
            )

    layout = layout_info(settings.layout_csv)
    bf_set = set(layout["antennas"])
    wired = set(layout["wired_antennas"])
    deployed = deployed_product(settings)
    deployed_set = set(deployed_cb_antennas(deployed.get("weights_file")).get("antennas") or [])
    # Beamforming-capable OR what is actually deployed (the layout column can
    # be edited after the last build; the live product is the other truth),
    # but ALWAYS inside the wired set: an antenna that is not wired has no
    # signal, and the weights stage would silently drop it into a near-empty
    # file that still passes the driver's own verification (casm-wiki
    # incidents.md 2026-08-31).
    allowed = (bf_set | deployed_set) & wired
    antennas = p.get("antennas")
    if not isinstance(antennas, (list, tuple)) or not antennas:
        raise ParamError("antennas must be a non-empty list of antenna ids")
    try:
        ants = sorted({int(a) for a in antennas})
    except (TypeError, ValueError) as exc:
        raise ParamError(f"antennas must be integers: {exc}") from exc
    not_wired = [a for a in ants if a not in wired]
    if not_wired:
        raise ParamError(
            f"antennas {not_wired} are not functional=1 (wired) in {layout['path']}"
        )
    outside = [a for a in ants if a not in allowed]
    if outside:
        raise ParamError(
            f"antennas {outside} are neither include_in_beamforming=1 in "
            f"{layout['path']} nor populated in the deployed CB product; the "
            f"weights stage would silently drop them"
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

    # The build ends by EXECUTING the IB generator in-process, so its path and
    # bytes are approved here, before a job row exists, not at the end of a
    # 10-minute solve.
    ib_refusal = ib_generator_refusal(
        settings.cal_ib_generator_script,
        settings.cal_ib_generator_script,
        settings.cal_ib_generator_sha256,
    )
    if ib_refusal is not None:
        raise ParamError(ib_refusal)

    prev_cal = p.get("prev_cal_path")
    if prev_cal is None:
        prev_cal = deployed.get("cal_file")
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


def run(params: dict[str, Any]) -> dict[str, Any]:
    """Job entry point (runs in the job subprocess, see jobs/run_job.py)."""
    settings = load_settings(params.get("config"))
    p = validate(params, settings)
    tag = p["tag"]
    out_dir = build_dir(settings, tag)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Layout provenance FIRST: the snapshot and its sha are what the upload
    # gate compares against, so they are written before anything can fail.
    #
    # Copy, then hash THE COPY, then solve against THE COPY. Hashing the live
    # ``current`` symlink and copying afterwards left a window in which the
    # file could change between the two, so the recorded sha described bytes
    # nobody ever used (2026-09-09 security review, finding 5). The copy's sha
    # is the ONE recorded hash: ``deploy_upload`` compares the live layout to
    # it AND re-hashes the copy itself.
    layout = layout_info(p["layout_csv"])
    snapshot = out_dir / "layout_snapshot.csv"
    resolved = Path(layout["resolved"])
    if not resolved.is_file():
        raise ParamError(f"the antenna layout {p['layout_csv']} does not resolve to a file")
    shutil.copyfile(resolved, snapshot)
    snapshot_sha = sha256_file(snapshot)
    if snapshot_sha != sha256_file(resolved):
        # The live file changed while it was being copied: the snapshot may be
        # torn, so there is nothing to bind the product to.
        raise ParamError(
            f"the antenna layout {resolved} changed while it was being snapshotted; retry"
        )
    layout["snapshot"] = str(snapshot)
    layout["live_sha256_at_build"] = layout["sha256"]
    layout["sha256"] = snapshot_sha
    # The solver reads the snapshot, never the live symlink.
    p["layout_csv_used"] = str(snapshot)

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
            layout_csv=p["layout_csv_used"],
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
            f"  layout {layout['path']} -> {layout['resolved']}\n"
            f"  solved against the snapshot {snapshot} sha256 {layout['sha256']}\n"
            f"  {len(p['antennas'])} antennas {p['antennas']} ref {p['ref_ant']}\n"
            f"  source {p['source']} {p['source_window'][0]} -> {p['source_window'][1]} UTC\n"
            f"  static {p['static_window']}\n"
            f"  prev_cal {p['prev_cal_path']}",
            flush=True,
        )
        out = run_recipe(recipe)

        # The canonical driver builds the CB product only; the IB mask is a
        # companion this job generates itself, paired to THIS build's own CB
        # file and antenna set (never the deployed one), from inside the same
        # tee'd section so its own prints land in build.log too.
        ib_file: str | None = None
        weights_file_for_ib = out.get("weights_file")
        if weights_file_for_ib and Path(weights_file_for_ib).is_file():
            print(
                f"[cal_build] generating IB mask from {weights_file_for_ib} via "
                f"{settings.cal_ib_generator_script}",
                flush=True,
            )
            ib_path = generate_ib_mask(
                weights_file_for_ib,
                out_dir,
                tag,
                len(p["antennas"]),
                settings.cal_ib_generator_script,
                settings.cal_ib_generator_sha256,
                settings.cal_ib_generator_script,
            )
            ib_file = str(ib_path)
        else:
            print(
                "[cal_build] no weights file produced; skipping IB mask generation",
                flush=True,
            )
    finally:
        sys.stdout = old_stdout
        tee.flush()
        tee.close()

    elapsed = time.time() - started
    peak_rss_mb = _peak_rss_mb()
    report = dict(out.get("report") or {})
    cal_file = out.get("cal_file")
    weights_file = out.get("weights_file")

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
