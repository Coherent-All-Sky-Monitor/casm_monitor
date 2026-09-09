"""Job subprocess entry point.

Invoked by the worker as::

    python -m casm_monitor.jobs.run_job <job_dir>

with ``<job_dir>/job.json`` holding ``{"id", "kind", "params"}``. stdout/stderr
are already redirected to ``<job_dir>/log.txt`` by the worker; the result is
written to ``<job_dir>/result.json`` and the exit code says whether the kind
raised.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

from .kinds import get_kind

try:
    import resource
except ImportError:  # pragma: no cover - resource is POSIX-only
    resource = None  # type: ignore[assignment]


def _apply_address_space_limit(limit_bytes: int | None) -> None:
    """Cap this subprocess's virtual address space at ``limit_bytes``.

    Belt and braces on top of the memory-bounded renderer itself: if a kind
    (currently only ``render_figures``) regresses back into loading something
    unbounded, the subprocess is killed (``MemoryError``/SIGSEGV -> non-zero
    exit, caught by the worker as a normal job failure) well under the job
    worker's 256G cgroup, instead of taking the whole service down again.
    Soft-fails (no-op, not an error) on platforms without ``resource`` (not
    POSIX) or without ``RLIMIT_AS`` (documented, not silently skipped).
    """
    if limit_bytes is None or resource is None:
        return
    if not hasattr(resource, "RLIMIT_AS"):
        print(
            "run_job: RLIMIT_AS not available on this platform; "
            "no per-kind address-space limit applied",
            file=sys.stderr,
        )
        return
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    except (ValueError, OSError) as exc:
        print(f"run_job: could not set RLIMIT_AS={limit_bytes}: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) != 1:
        print("usage: python -m casm_monitor.jobs.run_job <job_dir>", file=sys.stderr)
        return 2
    job_dir = Path(args[0])
    spec = json.loads((job_dir / "job.json").read_text())
    result_path = job_dir / "result.json"
    try:
        kind = get_kind(str(spec["kind"]))
        _apply_address_space_limit(kind.address_space_limit_bytes)
        result = kind.run(dict(spec.get("params") or {}))
        result_path.write_text(json.dumps(result, default=str, indent=1))
        return 0
    except Exception as exc:
        traceback.print_exc()
        result_path.write_text(
            json.dumps({"error": f"{type(exc).__name__}: {exc}"}, default=str, indent=1)
        )
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
