"""The status strip: which scalars appear, how they are labelled and graded.

One table drives ``/api/status`` and the WebSocket push. Each item names the
scalar it reads, the collector whose cadence defines its staleness, and optional
warn/error predicates. Grading, in increasing severity:

    ok      fresh and within limits
    warn    age > 2 x cadence, or the item's warn predicate fired
    stale   age > 5 x cadence (or never collected)
    error   the item's error predicate fired (a dead dependency)

Nothing is ever blank: a missing scalar is reported as ``stale`` with a null
value, per the plan's "never blank" rule.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from ..util import iso

GROUP_ORDER = ("obs", "weights", "search", "services", "sky", "node", "store")

_RANK = {"ok": 0, "warn": 1, "stale": 2, "error": 3}


def _is_zero(value: Any) -> bool:
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def _is_one(value: Any) -> bool:
    try:
        return float(value) == 1.0
    except (TypeError, ValueError):
        return False


def _above(limit: float) -> Callable[[Any], bool]:
    def check(value: Any) -> bool:
        try:
            return float(value) > limit
        except (TypeError, ValueError):
            return False

    return check


def _below(limit: float) -> Callable[[Any], bool]:
    def check(value: Any) -> bool:
        try:
            return float(value) < limit
        except (TypeError, ValueError):
            return False

    return check


@dataclass(frozen=True)
class StatusItem:
    key: str
    scalar: str
    label: str
    group: str
    cadence_key: str
    unit: str | None = None
    warn_if: Callable[[Any], bool] | None = None
    error_if: Callable[[Any], bool] | None = None


ITEMS: tuple[StatusItem, ...] = (
    # -- observation ----------------------------------------------------
    StatusItem("obs_utc_start", "obs.utc_start", "obs UTC_START", "obs", "obs"),
    StatusItem("obs_daemons_state", "obs.daemons_state", "medusa daemons", "obs", "obs"),
    StatusItem("obs_daemons_up", "obs.daemons_up", "daemons up", "obs", "obs"),
    StatusItem(
        "obs_lmc_ok", "obs.lmc_ok", "LMC :20300", "obs", "obs", error_if=_is_zero
    ),
    StatusItem("obs_sub_incoh", "obs.sub_incoh", "SUB_INCOH", "obs", "obs"),
    StatusItem(
        "obs_bf_scale_factor", "obs.bf_scale_factor", "bfcorr scale factor", "obs", "obs"
    ),
    StatusItem("obs_n_hella", "obs.n_hella", "hella processes", "obs", "obs"),
    # -- weights --------------------------------------------------------
    StatusItem("weights_product_id", "weights.product_id", "weights product", "weights", "weights"),
    StatusItem("weights_file", "weights.weights_file", "weights file", "weights", "weights"),
    StatusItem("weights_cal_file", "weights.cal_file", "cal file", "weights", "weights"),
    StatusItem("weights_scale", "weights.scale", "CB SCALE", "weights", "weights"),
    StatusItem("weights_ib_scale", "weights.ib_scale", "IB SCALE", "weights", "weights"),
    StatusItem("weights_ledger_date", "weights.ledger_date", "ledger row", "weights", "weights"),
    StatusItem(
        "weights_registry_mismatch",
        "weights.registry_mismatch",
        "registry vs ledger",
        "weights",
        "weights",
        warn_if=_is_one,
    ),
    # -- search (hella thresholds) --------------------------------------
    StatusItem("hella_corr1_snr", "hella.corr1.snr", "hella SNR (corr1)", "search", "hella"),
    StatusItem("hella_corr1_dm_min", "hella.corr1.dm_min", "hella DM_MIN (corr1)", "search", "hella", unit="pc/cm3"),
    StatusItem("hella_corr1_n_jobs", "hella.corr1.n_jobs", "hella jobs (corr1)", "search", "hella"),
    StatusItem("hella_corr2_snr", "hella.corr2.snr", "hella SNR (corr2)", "search", "hella_corr2"),
    StatusItem("hella_corr2_dm_min", "hella.corr2.dm_min", "hella DM_MIN (corr2)", "search", "hella_corr2", unit="pc/cm3"),
    StatusItem("hella_corr2_n_jobs", "hella.corr2.n_jobs", "hella jobs (corr2)", "search", "hella_corr2"),
    # -- services -------------------------------------------------------
    StatusItem("redis_ok", "services.redis_ok", "redis", "services", "services", error_if=_is_zero),
    StatusItem("kafka_ok", "services.kafka_ok", "kafka broker", "services", "services", error_if=_is_zero),
    StatusItem(
        "kafka_advancing",
        "services.kafka_advancing",
        "kafka offsets advancing",
        "services",
        "services",
        warn_if=_is_zero,
    ),
    StatusItem(
        "kafka_bp_ok",
        "kafka_bp.ok",
        "kafka bandpass consumer",
        "services",
        "kafka_bp_frame",
        error_if=_is_zero,
    ),
    StatusItem(
        "kafka_bp_subbands_ok",
        "kafka_bp.subbands_ok",
        "kafka subbands (of 6)",
        "services",
        "kafka_bp_frame",
        warn_if=_below(6.0),
    ),
    StatusItem("t2d_ok", "services.t2d_ok", "t2d", "services", "services", warn_if=_is_zero),
    StatusItem("zapdos_ok", "services.zapdos_ok", "zapdos ssh", "services", "zapdos", warn_if=_is_zero),
    # -- sky ------------------------------------------------------------
    StatusItem("sun_alt", "sky.sun.alt_deg", "Sun altitude", "sky", "sky", unit="deg"),
    StatusItem("sun_next_max", "sky.sun.next_max_utc", "Sun next max", "sky", "sky"),
    StatusItem("cyg_a_alt", "sky.cyg_a.alt_deg", "Cyg A altitude", "sky", "sky", unit="deg"),
    StatusItem("cyg_a_next_max", "sky.cyg_a.next_max_utc", "Cyg A next max", "sky", "sky"),
    StatusItem("cas_a_alt", "sky.cas_a.alt_deg", "Cas A altitude", "sky", "sky", unit="deg"),
    StatusItem("cas_a_next_max", "sky.cas_a.next_max_utc", "Cas A next max", "sky", "sky"),
    # -- node -----------------------------------------------------------
    StatusItem("nvme3_pct_used", "disk.mnt_nvme3.pct_used", "/mnt/nvme3 used", "node", "disks", unit="%", warn_if=_above(90.0)),
    StatusItem("nvme4_pct_used", "disk.mnt_nvme4.pct_used", "/mnt/nvme4 used", "node", "disks", unit="%", warn_if=_above(90.0)),
    StatusItem("nvme5_pct_used", "disk.mnt_nvme5.pct_used", "/mnt/nvme5 used", "node", "disks", unit="%", warn_if=_above(90.0)),
    StatusItem("nvme3_free_gb", "disk.mnt_nvme3.free_gb", "/mnt/nvme3 free", "node", "disks", unit="GB"),
    StatusItem("gpu_util_max", "gpu.util_max_pct", "GPU utilisation (max)", "node", "gpus", unit="%"),
    # -- store ----------------------------------------------------------
    StatusItem("store_bytes", "store.bytes", "store size", "store", "store", unit="B"),
    StatusItem("store_sqlite_bytes", "store.sqlite_bytes", "sqlite size", "store", "store", unit="B"),
    StatusItem("store_shards", "store.shards", "committed shards", "store", "store"),
)


def _grade(item: StatusItem, value: Any, age_s: float | None, cadence_s: float) -> str:
    if age_s is None:
        return "stale"
    state = "ok"
    if age_s > 5 * cadence_s:
        state = "stale"
    elif age_s > 2 * cadence_s:
        state = "warn"
    if item.warn_if is not None and item.warn_if(value):
        state = state if _RANK[state] > _RANK["warn"] else "warn"
    if item.error_if is not None and item.error_if(value):
        state = "error"
    return state


def build_status(
    latest: dict[str, dict[str, Any]],
    cadences: dict[str, float],
    *,
    now: float | None = None,
    items: tuple[StatusItem, ...] = ITEMS,
) -> dict[str, Any]:
    """Assemble the /api/status payload from the newest scalar per name."""
    t_now = time.time() if now is None else now
    out_items: dict[str, Any] = {}
    for item in items:
        row = latest.get(item.scalar)
        value = None if row is None else row["value"]
        ts = None if row is None else row["ts"]
        age = None if ts is None else max(0.0, t_now - float(ts))
        cadence = float(cadences.get(item.cadence_key, 60.0))
        out_items[item.key] = {
            "value": value,
            "ts": iso(ts),
            "age_s": None if age is None else round(age, 1),
            "state": _grade(item, value, age, cadence),
            "label": item.label,
            "unit": item.unit,
            "group": item.group,
        }
    groups = [
        {"name": name, "keys": [i.key for i in items if i.group == name]}
        for name in GROUP_ORDER
    ]
    return {"ts": iso(t_now), "items": out_items, "groups": groups}


def collect_age_s(heartbeats: dict[str, dict[str, Any]], now: float | None = None) -> float | None:
    """Seconds since any collector last succeeded (None if none ever did)."""
    t_now = time.time() if now is None else now
    oks = [hb["last_ok"] for hb in heartbeats.values() if hb.get("last_ok")]
    if not oks:
        return None
    return round(max(0.0, t_now - max(oks)), 1)
