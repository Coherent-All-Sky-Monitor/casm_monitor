"""Job kinds registry.

A job kind is a pure function of its params that runs inside the job
subprocess, prints to its log and returns a JSON-serialisable result. M0 shipped
``noop``, which exists so the whole queue/lease/cancel/timeout path can be
tested end to end; M1 added ``snap_read`` and ``render_figures``; M3 adds
``cal_build``, ``deploy_stage`` and ``deploy_upload``.

Nothing in this registry may touch the hardware: no SNAP ``program_*``,
``health_sweep``, ``set_coeffs``, ``--do_sync``, and no medusa restart. The one
path that writes to the live pipeline is ``deploy_upload``, which pushes DADA
payloads to the beamformer FIFOs and only runs behind the full safeguard list
in :mod:`casm_monitor.jobs.deploy` (casm-wiki
``decisions/2026-09-09-monitor-upload-button.md``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class JobKind:
    name: str
    run: Callable[[dict[str, Any]], dict[str, Any]]
    timeout_s: float = 3600.0
    description: str = ""
    # RLIMIT_AS (bytes) applied to the job subprocess before it runs, or None
    # for no per-kind cap (see jobs/run_job.py). This is the guard on top of
    # the memory-bounded renderer itself: a bug that regresses the bound still
    # gets killed well under the worker's 256G cgroup instead of taking the
    # whole service down.
    address_space_limit_bytes: int | None = None


def _noop(params: dict[str, Any]) -> dict[str, Any]:
    """Sleep ``seconds`` (default 1), reporting progress to the job log."""
    seconds = float(params.get("seconds", 1.0))
    started = time.time()
    print(f"noop: sleeping {seconds:g} s", flush=True)
    remaining = seconds
    while remaining > 0:
        step = min(1.0, remaining)
        time.sleep(step)
        remaining -= step
        print(f"noop: {seconds - remaining:.1f}/{seconds:g} s", flush=True)
    elapsed = time.time() - started
    print(f"noop: done in {elapsed:.3f} s", flush=True)
    return {"slept_s": seconds, "elapsed_s": round(elapsed, 3), "message": params.get("message")}


def _render_figures(params: dict[str, Any]) -> dict[str, Any]:
    """Render the Vis and SNAPs tab figures (moved off the collector, 2026-09-09:
    it OOM'd ``casm-monitor-collect.service``, MemoryMax=8G).
    """
    from .render_figures import run as run_render_figures

    return run_render_figures(params)


def _cal_build(params: dict[str, Any]) -> dict[str, Any]:
    """One canonical cal + weights product through the driver (M3)."""
    from .cal_build import run as run_cal_build

    return run_cal_build(params)


def _deploy_stage(params: dict[str, Any]) -> dict[str, Any]:
    """Dry-run deploy of a build into its own stage dir (no network side effects)."""
    from .deploy import run_stage

    return run_stage(params)


def _deploy_upload(params: dict[str, Any]) -> dict[str, Any]:
    """The gated live upload; refuses unless every safeguard holds."""
    from .deploy import run_upload

    return run_upload(params)


def _snap_read(params: dict[str, Any]) -> dict[str, Any]:
    """One serialized read-only pass over the SNAP boards through zapdos.

    Imported lazily so the registry (and therefore the web process) does not
    pull numpy/zarr in just to list the kinds.
    """
    from .snap_read import run as run_snap_read

    return run_snap_read(params)


KINDS: dict[str, JobKind] = {
    "noop": JobKind(
        name="noop",
        run=_noop,
        timeout_s=600.0,
        description="sleep N seconds; smoke-tests the job worker",
    ),
    "snap_read": JobKind(
        name="snap_read",
        run=_snap_read,
        # 7 boards x 60 s budget plus ssh and store writes, with margin; the
        # remote script enforces the per-board budget itself.
        timeout_s=900.0,
        description=(
            "read-only SNAP board read via zapdos (spectra, ADC stats, EQ, PPS); "
            'params {"ips": [...]|null, "reason": "scheduled"|"manual"}'
        ),
    ),
    "cal_build": JobKind(
        name="cal_build",
        run=_cal_build,
        # A 47-integration solve plus the diagnostics, the Cyg A beam check and
        # an executed notebook. An hour is generous against the ~10 min the
        # 18-antenna one-hour window measures at, and the driver aborts long
        # before that on a bad window.
        timeout_s=3600.0,
        # A solve materialises ~9.5 GB through casm_io; 64 GB is well above the
        # measured peak and well under the worker's 256G cgroup, so a regression
        # dies as a failed job instead of an OOM on the node.
        address_space_limit_bytes=64 * 1024 * 1024 * 1024,
        description=(
            "canonical cal + 512-beam exact-grid weights via "
            "bf_weights_generator.make_cal_and_weights; params {source, source_window, "
            "static_window, antennas, ref_ant, tag, prev_cal_path}"
        ),
    ),
    "deploy_stage": JobKind(
        name="deploy_stage",
        run=_deploy_stage,
        timeout_s=900.0,
        description=(
            "dry-run deploy_bf_weights.py (no --upload) into the build's stage dir, "
            'record md5s + the upload command; params {"build_tag": ...}'
        ),
    ),
    "deploy_upload": JobKind(
        name="deploy_upload",
        run=_deploy_upload,
        timeout_s=900.0,
        description=(
            "the gated live weights upload (human click only); params "
            '{"build_tag", "confirm_tag", "save_defaults", "note"}'
        ),
    ),
    "render_figures": JobKind(
        name="render_figures",
        run=_render_figures,
        # 2026-09-08: raised from 900 s -- even after the imshow/single-render
        # figure optimisation, 33 renders/set at MAX_WORKERS render concurrency
        # (jobs/render_figures.py) need headroom above the synthetic-cube
        # benchmark's per-figure numbers (see bench_render.py) on the live
        # 24-input wired set. The renderer itself now writes manifests
        # incrementally per kind (default view first), so a job that still
        # overruns this leaves fresh, usable figures rather than nothing.
        timeout_s=1800.0,
        # Belt and braces on top of the memory-bounded renderer itself (never
        # reads vis_full, never concatenates a whole vis_avg8 window): 16 GB
        # is generously above the ~3 GB/target peak RSS this renders at.
        address_space_limit_bytes=16 * 1024 * 1024 * 1024,
        description=(
            "render the Vis + SNAPs tab figure PNGs and manifests; "
            'params {"targets": ["vis", "snaps"], "reason": "scheduled"|"manual"|"board_read"}'
        ),
    ),
}


def get_kind(name: str) -> JobKind:
    if name not in KINDS:
        raise KeyError(f"unknown job kind {name!r}; known: {sorted(KINDS)}")
    return KINDS[name]
