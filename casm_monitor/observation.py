"""Small observation provenance records shared by the worker and read-only API."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .collectors.weights import load_product
from .store.shards import ensure_contained
from .util import parse_iso


def cache_dir(settings):
    if settings.observation_cache_root is not None:
        return Path(settings.observation_cache_root).resolve()
    return ensure_contained(settings.store_root / "figures" / "observation", settings.store_root)


def read_json(path):
    try:
        if Path(path).stat().st_size > 2_000_000:
            return {}
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def recorded_product(settings, latest):
    """Resolve only the collector's live-event product, never an unrelated build."""
    def value(key):
        return latest.get(key, {}).get("value")
    pid = value("weights.product_id")
    result = {"product_id": pid, "source": value("weights.product_source"),
              "live_event_utc": value("weights.live_event_utc"),
              "registry_mismatch": value("weights.registry_mismatch"),
              "path": None, "evidence": "Collector's latest registry live-event association; not a new node-payload verification."}
    streams = latest_stream_events(settings.registry_dir / "live_events.jsonl")
    result["per_stream"] = streams
    ids = {s.get("product_id") for s in streams}
    complete = len(streams) == 6 and len(ids) == 1 and None not in ids
    result["stream_evidence_state"] = "consistent" if complete else "mixed_or_missing"
    if not complete or pid not in ids:
        result["error"] = "Latest evidence is mixed, missing, or differs from the collector association"
        return result
    if result["source"] != "live_event" or not pid or not re.fullmatch(r"[A-Za-z0-9_.-]+", str(pid)):
        result["error"] = "No collector-confirmed live-event product association"
        return result
    product = load_product(settings.registry_dir, str(pid))
    if not product or not product.get("h5_path"):
        result["error"] = "Live-event product record unavailable"
        return result
    result["path"] = product["h5_path"]
    return result


def latest_stream_events(path):
    """Latest dated association for each of six streams in a <=1 MiB tail."""
    try:
        with Path(path).open("rb") as f:
            size = f.seek(0, 2)
            start = max(0, size - 1024**2)
            f.seek(start)
            lines = f.read(1024**2).splitlines()
            if start:
                lines = lines[1:]
    except OSError:
        return []
    found = {}
    for line in lines:
        try:
            event = json.loads(line)
            stream = int(event.get("stream", -1))
            stamp = parse_iso(event.get("utc"))
            if stream not in range(6) or stamp is None:
                continue
            if stream not in found or stamp >= found[stream][0]:
                found[stream] = (stamp, {k: event.get(k) for k in ("stream", "node", "utc", "source", "product_id", "payload_md5", "evidence")})
        except (ValueError, TypeError):
            continue
    return [found[s][1] for s in sorted(found)]


def file_identity(path):
    p = Path(path).resolve()
    stat = p.stat()
    return {"path": str(p), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def inspected_deployment(settings, latest):
    """Small cached payload inspection, valid only for the current association."""
    product = recorded_product(settings, latest)
    cached = read_json(cache_dir(settings) / "membership.json")
    try:
        matching = (bool(product.get("path"))
                    and cached.get("identity") == file_identity(product["path"])
                    and cached.get("product_id") == product.get("product_id")
                    and cached.get("path") == product.get("path")
                    and cached.get("inspection_state") == "complete"
                    and not product.get("registry_mismatch"))
    except OSError:
        matching = False
    return ({**cached, **product} if matching else
            {**product, "inspection_state": "pending", "beams": [], "antennas": []})
