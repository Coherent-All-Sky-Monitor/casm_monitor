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
