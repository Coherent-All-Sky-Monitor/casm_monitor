"""Job kinds registry.

A job kind is a pure function of its params that runs inside the job
subprocess, prints to its log and returns a JSON-serialisable result. M0 ships
one kind, ``noop``, which exists so the whole queue/lease/cancel/timeout path
can be tested end to end; M3/M4 add the calibration and imaging kinds here.

Nothing in this registry may touch the hardware: no SNAP ``program_*``,
``health_sweep``, ``set_coeffs``, ``--do_sync``, and no medusa restart.
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
    "render_figures": JobKind(
        name="render_figures",
        run=_render_figures,
        timeout_s=900.0,
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
