"""Which beamforming weights are actually loaded, and does the ledger agree.

Two independent sources, deliberately cross-checked:

* the weights registry ``/mnt/nvme5/casm_pipeline/weights/registry/``:
  ``products/<product_id>.json`` (``h5_path``, ``stream_payload_md5``,
  ``meta.scale`` / ``meta.ib_scale`` / ``meta.utc_start_header``) and
  ``live_events.jsonl`` (one row per stream per apply, with ``product_id``),
  plus ``alerts.jsonl`` for unregistered payloads.
* the wiki ledger ``deployed_weights.csv``, whose LAST ROW is the standing
  authority for what is deployed (``weights_file``, ``cal_file``, date).

If the newest live event's product does not match the ledger's last row, the
``weights.registry_mismatch`` flag goes to 1 and an event is raised. The
collector only reads: it never registers, uploads or edits anything.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from ..util import parse_iso
from .base import Collector, CollectorContext


def read_last_ledger_row(path: str | Path) -> dict[str, str] | None:
    """Last data row of deployed_weights.csv (multi-line quoted notes safe)."""
    p = Path(path)
    if not p.is_file():
        return None
    with p.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    return rows[-1] if rows else None


def ledger_weights_basename(row: dict[str, str] | None) -> str | None:
    """The CB weights file of a ledger row, without the '(+ ib_...)' suffix."""
    if not row:
        return None
    field = (row.get("weights_file") or "").strip()
    if not field:
        return None
    primary = field.split("(")[0].strip()
    return Path(primary).name or None


def read_live_events(path: str | Path, tail: int = 200) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        return []
    lines = p.read_text(errors="replace").splitlines()[-tail:]
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def newest_product(registry_dir: str | Path) -> dict[str, Any] | None:
    """Product JSON with the newest ``recorded_utc`` (mtime as a fallback)."""
    pdir = Path(registry_dir) / "products"
    best: tuple[float, dict[str, Any]] | None = None
    if not pdir.is_dir():
        return None
    for path in pdir.glob("*.json"):
        try:
            obj = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        stamp = parse_iso(obj.get("recorded_utc")) or path.stat().st_mtime
        if best is None or stamp > best[0]:
            best = (stamp, obj)
    return None if best is None else best[1]


def load_product(registry_dir: str | Path, product_id: str) -> dict[str, Any] | None:
    path = Path(registry_dir) / "products" / f"{product_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


class WeightsCollector(Collector):
    """Deployed weights product vs the wiki ledger."""

    name = "weights"
    default_cadence_s = 120.0
    timeout_s = 60.0

    def collect(self, ctx: CollectorContext) -> None:
        registry_dir = Path(ctx.settings.registry_dir)
        events = read_live_events(registry_dir / "live_events.jsonl")
        live = None
        if events:
            live = max(
                events,
                key=lambda e: (parse_iso(e.get("utc")) or 0.0, parse_iso(e.get("recorded_utc")) or 0.0),
            )

        # The product the array is actually running is the one the newest live
        # event names. If its JSON is missing, that is a registry fault to
        # report — never a reason to show the newest unrelated product instead.
        product: dict[str, Any] | None = None
        product_missing = 0
        live_product_id = str(live.get("product_id")) if (live and live.get("product_id")) else None
        if live_product_id:
            product = load_product(registry_dir, live_product_id)
            if product is None:
                product_missing = 1
                source = "missing"
            else:
                source = "live_event"
        else:
            product = newest_product(registry_dir)
            source = "newest_registry_product" if product else "none"
        ctx.scalar("weights.product_source", source)
        seen_before = ctx.store.latest_scalar("weights.product_missing") is not None
        detail = {"product_id": live_product_id, "live_event_utc": (live or {}).get("utc")}
        changed = ctx.on_change(
            "weights.product_missing",
            product_missing,
            kind="registry_product_missing",
            severity="error",
            subject=live_product_id or "weights registry",
            detail=detail,
        )
        if product_missing:
            ctx.scalar("weights.missing_product_id", live_product_id or "unknown")
            if not changed and not seen_before:
                # First ever collect and it is already broken: say so once.
                ctx.event(
                    "registry_product_missing",
                    severity="error",
                    subject=live_product_id or "weights registry",
                    detail=detail,
                )

        ledger = read_last_ledger_row(ctx.settings.deployed_weights_csv)
        ledger_weights = ledger_weights_basename(ledger)
        ledger_cal = Path((ledger or {}).get("cal_file", "").split("(")[0].strip()).name or None
        ledger_date = (ledger or {}).get("date_deployed")

        product_id = str(product.get("product_id")) if product else "unknown"
        h5_path = str(product.get("h5_path")) if product else None
        meta = (product or {}).get("meta") or {}

        ctx.on_change(
            "weights.product_id",
            product_id,
            kind="weights_change",
            severity="info",
            subject="deployed weights product",
            detail={"h5_path": h5_path, "ledger_date": ledger_date},
        )
        ctx.scalar("weights.weights_file", Path(h5_path).name if h5_path else "unknown")
        ctx.scalar("weights.cal_file", ledger_cal or "unknown")
        ctx.scalar("weights.ledger_date", ledger_date or "unknown")
        ctx.scalar("weights.ledger_weights_file", ledger_weights or "unknown")
        if meta.get("scale") is not None:
            ctx.scalar("weights.scale", float(meta["scale"]))
        if meta.get("ib_scale") is not None:
            ctx.scalar("weights.ib_scale", float(meta["ib_scale"]))
        if meta.get("utc_start_header"):
            ctx.scalar("weights.utc_start_header", str(meta["utc_start_header"]))
        if live:
            ctx.scalar("weights.live_event_utc", str(live.get("utc") or "unknown"))
            ctx.scalar("weights.live_event_source", str(live.get("source") or "unknown"))
            ctx.scalar("weights.n_live_events", len(events))

        registry_name = Path(h5_path).name if h5_path else None
        mismatch = 1 if (registry_name and ledger_weights and registry_name != ledger_weights) else 0
        if registry_name is None or ledger_weights is None or product_missing:
            # A product the live event names but the registry does not hold is
            # a mismatch in its own right.
            mismatch = 1
        ctx.on_change(
            "weights.registry_mismatch",
            mismatch,
            kind="registry_mismatch",
            severity="warn",
            subject="weights registry vs ledger",
            detail={
                "registry_weights_file": registry_name,
                "ledger_weights_file": ledger_weights,
                "product_id": product_id,
                "product_missing": product_missing,
                "product_source": source,
            },
        )

        alerts = read_live_events(registry_dir / "alerts.jsonl", tail=20)
        if alerts:
            newest = alerts[-1]
            ctx.scalar("weights.last_alert_kind", str(newest.get("kind") or "unknown"))
            ctx.scalar("weights.last_alert_utc", str(newest.get("utc") or "unknown"))
