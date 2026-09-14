"""Bounded, read-only injection evidence for the observation page.

Search outcomes and replay availability are independent. A replay contains a
synthetic pulse added to recorded background, not the live injected stream.
No T2 convenience connection is used: it initializes/migrates the live store.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from casm_t2 import inject_outcome
from casm_t2.hella_kernel import kernel_fwhm_ms
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..config import Settings

EVENTS_ROOT = Path("/mnt/nvme3/T3/EVENTS")
MAX_ROWS = 5000
RECENT_ROWS = 30
ID_RE = re.compile(r"inj_\d{8}_\d{4,8}\Z")
ARTIFACT_TYPES = {"png": "image/png", "json": "application/json", "fil": "application/octet-stream"}
REPLAY_CAPTION = (
    "Synthetic pulse re-added to recorded background; live recovery is established "
    "by the matched search cluster, not this replay."
)
FIELDS = (
    "id,file_id,inject_utc,beam,dm,sigma_ms,inject_snr,rec_snr,rec_dm,rec_beam,"
    "rec_width,rec_lead_s,outcome,fail_reason,n_t1_trials,matched_cluster,"
    "gate_t1,gate_t2,gate_trigger,sub_incoh,replay_png"
)


def _artifact_path(root: Path, file_id: str, kind: str) -> Path | None:
    """Resolve only fixed event filenames; refuse escapes through symlinks."""
    if not ID_RE.fullmatch(file_id) or kind not in ARTIFACT_TYPES:
        return None
    try:
        root = root.resolve()
        path = (root / file_id / f"{file_id}.{kind}").resolve()
        if path.is_relative_to(root) and path.is_file():
            return path
    except (OSError, RuntimeError):
        # Missing mounts, unreadable files and symlink loops are unavailable
        # products; they must not hide the independently recorded recovery.
        pass
    return None


def _counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {key: 0 for key in inject_outcome.ALL}
    counts.update(pending=0, unknown=0)
    for row in rows:
        outcome = row["outcome"]
        key = outcome if outcome in inject_outcome.ALL else ("pending" if not outcome else "unknown")
        counts[key] += 1
    counts["completed_fired"] = sum(counts[key] for key in (inject_outcome.RECOVERED, *inject_outcome.MISSES))
    counts["recovery_fraction"] = (
        counts[inject_outcome.RECOVERED] / counts["completed_fired"]
        if counts["completed_fired"] else None
    )
    return counts


def _shot(row: dict[str, Any], root: Path) -> dict[str, Any]:
    result = dict(row)
    result["injected_fwhm_ms"] = row["sigma_ms"] * 2.354820045 if row["sigma_ms"] is not None else None
    width = row["rec_width"]
    result["recovered_fwhm_ms"] = kernel_fwhm_ms(int(width)) if width is not None and 0 <= width <= 6 else None
    artifacts = {}
    for kind in ARTIFACT_TYPES:
        path = _artifact_path(root, str(row["file_id"]), kind)
        artifacts[kind] = {
            "available": path is not None,
            "url": f"/api/observation/injections/artifact/{row['file_id']}/{kind}" if path else None,
        }
    result["replay"] = {
        "available": artifacts["png"]["available"],
        "recorded": bool(row["replay_png"]),
        "caption": REPLAY_CAPTION,
        "artifacts": artifacts,
    }
    result["evidence_note"] = (
        "T1 trial availability is unknown; this classification does not demonstrate zero trials."
        if row["outcome"] == inject_outcome.MISSED_T1 and row["n_t1_trials"] is None else None
    )
    return result


def build_injection_overview(
    settings: Settings, *, now: datetime | None = None, events_root: Path = EVENTS_ROOT,
) -> dict[str, Any]:
    """Read at most 5001 ledger rows in seven UTC calendar days.

    Counts describe today in UTC; trend includes today and six preceding days.
    An unset outcome is exposed separately as pending/legacy bookkeeping.
    If the row budget is exceeded, counts are explicitly incomplete.
    """
    now = now or datetime.now(timezone.utc)
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=6)
    result: dict[str, Any] = {
        "status": "unavailable", "as_of_utc": now.isoformat(),
        "window_start_utc": today.isoformat(), "trend_start_utc": start.isoformat(),
        "counts": _counts([]), "counts_complete": False, "trend": [],
        "latest_completed": None, "recent": [],
        "pending_note": "Unset outcomes are pending or legacy bookkeeping, not search misses.",
        "recovery_note": "Completed, fired shots only; compare like DM, width, S/N and observing conditions.",
        "source": str(settings.t2_db),
    }
    try:
        connection = sqlite3.connect(Path(settings.t2_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            # Indexed ISO-T bounds; no date() wrapper or whole-table aggregation.
            rows = [dict(row) for row in connection.execute(
                f"SELECT {FIELDS} FROM injections WHERE inject_utc >= ? AND inject_utc <= ? "
                "ORDER BY inject_utc DESC LIMIT ?",
                (start.isoformat(), now.isoformat(), MAX_ROWS + 1),
            )]
        finally:
            connection.close()
    except (sqlite3.Error, OSError) as exc:
        result["reason"] = f"Injection ledger unavailable: {exc}"
        return result
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    result.update(status="partial" if truncated else "ok", counts_complete=not truncated)
    if truncated:
        result["reason"] = "Seven-day row budget exceeded; displayed counts are incomplete."
    result["counts"] = _counts([row for row in rows if row["inject_utc"][:10] == today.date().isoformat()])
    result["trend"] = [
        {"date_utc": day.date().isoformat(), **_counts([row for row in rows if row["inject_utc"][:10] == day.date().isoformat()])}
        for day in (start + timedelta(days=i) for i in range(7))
    ]
    result["recent"] = [_shot(row, events_root) for row in rows[:RECENT_ROWS]]
    latest = next((row for row in rows if row["outcome"] in inject_outcome.ALL), None)
    result["latest_completed"] = _shot(latest, events_root) if latest else None
    return result


def build_router(settings: Settings, *, events_root: Path = EVENTS_ROOT) -> APIRouter:
    """Serve existing injection artifacts only, never generate or post them."""
    router = APIRouter(prefix="/api/observation/injections")

    @router.get("/artifact/{file_id}/{kind}")
    def artifact(file_id: str, kind: str) -> FileResponse:
        path = _artifact_path(events_root, file_id, kind)
        if path is None:
            raise HTTPException(status_code=404, detail="Saved injection artifact unavailable")
        return FileResponse(
            path, media_type=ARTIFACT_TYPES[kind],
            filename=path.name if kind == "fil" else None,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    return router
