"""Science-first overview. GETs inspect small records, never scientific arrays."""
from __future__ import annotations

import time
import warnings
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request

from ..collectors.rowmap import read_layout
from ..observation import cache_dir, inspected_deployment, read_json
from ..store.shards import ensure_contained
from ..util import iso, parse_iso
from .figures import _png_response
from .injection_overview import build_injection_overview, build_router as injection_router


def clock_and_location(now):
    from astropy import units as u
    from astropy.time import Time
    from astropy.utils import iers
    from casm_io.constants import OVRO_LAT_DEG, OVRO_LON_DEG, OVRO_ELEV_M
    local = datetime.fromtimestamp(now, timezone.utc).astimezone(ZoneInfo("America/Los_Angeles"))
    clock = {"utc": iso(now), "local": local.isoformat(timespec="seconds"),
             "timezone": "America/Los_Angeles", "lst": None,
             "lst_note": "Mean sidereal time using installed offline Earth-orientation tables; display only."}
    try:
        with warnings.catch_warnings(), iers.conf.set_temp("auto_download", False), iers.conf.set_temp("auto_max_age", None):
            warnings.simplefilter("ignore", iers.IERSWarning)
            clock["lst"] = Time(now, format="unix").sidereal_time("mean", longitude=OVRO_LON_DEG * u.deg).to_string(unit=u.hour, sep=":", precision=0, pad=True)
    except Exception:
        clock["lst_note"] = "Offline sidereal time unavailable"
    return clock, {"name": "CASM · OVRO", "latitude_deg": OVRO_LAT_DEG,
                   "longitude_deg": OVRO_LON_DEG, "elevation_m": OVRO_ELEV_M}


def build_observation(settings, reader, *, now=None):
    now = time.time() if now is None else now
    latest = reader.latest_scalars()
    def scalar(name):
        row = latest.get(name, {})
        ts = row.get("ts")
        return {"value": row.get("value"), "observed_at": iso(ts),
                "age_s": max(0, now - ts) if ts is not None else None}
    clock, location = clock_and_location(now)
    deployment = inspected_deployment(settings, latest)
    matching = deployment.get("inspection_state") == "complete"
    members = set(deployment.get("antennas", [])) if matching else set()
    positions = {p["antenna"]: p for p in deployment.get("positions", [])} if matching else {}
    points = []
    error = None
    try:
        for row in read_layout(settings.layout_csv):
            aid = int(row["antenna"])
            wired = row.get("functional") == "1"
            if not wired and not row.get("row") and aid not in members:
                continue
            point = {"antenna": aid, "name": f"ant {aid} ({row.get('row', '')}{row.get('col', '')}, S{row.get('snap')}A{row.get('adc')})",
                     "east_m": float(row["x"]), "north_m": float(row["y"]),
                     "wired": wired, "intended": row.get("include_in_beamforming") == "1",
                     "deployed": aid in members if matching else None,
                     "packet_idx": int(row["packet_idx"]), "station": row.get("row", "") + row.get("col", ""),
                     "geometry_source": "current layout"}
            if aid in positions:
                point["deployed_east_m"] = positions[aid]["east_m"]
                point["deployed_north_m"] = positions[aid]["north_m"]
                point["slot_identity_matches"] = point["packet_idx"] == positions[aid]["slot"]
                point["geometry_matches"] = (abs(point["east_m"] - positions[aid]["east_m"]) < 1e-4
                                               and abs(point["north_m"] - positions[aid]["north_m"]) < 1e-4)
                if not point["slot_identity_matches"]:
                    point["deployed"] = None
            points.append(point)
        known = {p["antenna"] for p in points}
        for aid in sorted(members - known):
            p = positions[aid]
            points.append({"antenna": aid, "name": f"ant {aid} (product epoch)", "east_m": p["east_m"],
                           "north_m": p["north_m"], "wired": None, "intended": None, "deployed": True,
                           "geometry_source": "weights product"})
    except (OSError, ValueError, KeyError) as exc:
        error = str(exc)
    solar = read_json(cache_dir(settings) / "solar.json") or {"status": "unavailable", "caption": "No solar-context product rendered yet"}
    solar.setdefault("caption", "No solar-context product rendered yet; see availability detail.")
    end = parse_iso(solar.get("observed_end"))
    solar["age_s"] = max(0, now - end) if end else None
    solar["stale"] = end is None or now - end > 3600
    if (cache_dir(settings) / "solar.png").is_file():
        solar["image_url"] = "/api/observation/solar.png?v=" + str(solar.get("rendered_at", ""))
    else:
        solar["image_url"] = None
    return {"clock": clock, "location": location,
            "observation": {"id": scalar("obs.utc_start")["value"], "identity": scalar("obs.utc_start"),
                            "state": scalar("obs.daemons_state"), "vis_age": scalar("vis.age_s")},
            "layout": {"path": str(settings.layout_csv.resolve()), "points": points, "error": error,
                       "counts": {"wired": sum(p["wired"] is True for p in points),
                                  "intended": sum(p["intended"] is True for p in points),
                                  "deployed_union": len(members) if matching else None},
                       "note": "Wired and intended are current-layout claims. Deployed is the recorded product's union; select a beam for its actual populated slots. Antenna IDs are layout-epoch specific; product coordinates are provided separately."},
            "deployment": deployment, "solar": solar,
            "injections": build_injection_overview(settings, now=datetime.fromtimestamp(now, timezone.utc))}


def build_router(settings, reader):
    router = APIRouter()
    router.include_router(injection_router(settings))

    @router.get("/api/observation")
    def observation():
        return build_observation(settings, reader)

    @router.get("/api/observation/solar.png")
    def solar_png(request: Request):
        path = ensure_contained(cache_dir(settings) / "solar.png", cache_dir(settings))
        if not path.is_file():
            raise HTTPException(404, "No solar-context figure")
        return _png_response(path, request)

    return router
