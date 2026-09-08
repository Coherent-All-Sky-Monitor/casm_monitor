"""Small shared helpers: UTC time formatting and subprocess capture."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone


def iso(ts: float | None) -> str | None:
    """Unix seconds -> ISO-8601 UTC string with a Z suffix."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def parse_iso(text: str | None) -> float | None:
    """ISO-8601 (Z or offset, naive treated as UTC) -> unix seconds."""
    if not text:
        return None
    s = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def utc_start_to_unix(utc_start: str | None) -> float | None:
    """Correlator UTC_START ('2026-09-04-08:42:33') -> unix seconds."""
    if not utc_start:
        return None
    try:
        dt = datetime.strptime(utc_start.strip(), "%Y-%m-%d-%H:%M:%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc).timestamp()


def run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    """Run a command, capture output; never raises on a non-zero exit."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s: {' '.join(cmd)}"
    except OSError as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr
