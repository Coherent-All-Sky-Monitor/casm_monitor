"""The SNAPs tab API: boards, the live Kafka bandpass, history and trends.

Read-only by construction: every route reads the layout CSVs (re-read per call
so a ``casm-layout apply`` shows up without a restart), the collector's
latest-frame mirror and the store's read-only handle. No route contacts a
board, the broker or zapdos.

Two endpoints of the contract belong to the board-read half of M1
(``casm_monitor/collectors/snapread.py`` and the ``snap_read`` job kind, owned
by another agent): ``GET``/``POST /api/snaps/board-read`` answer 501 here so
the frontend gets a clean, documented response until they land.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException, Query

from ..collectors import rowmap
from ..collectors.kafka_bp import (
    DB_FLOOR,
    FRAME_CADENCE_S,
    STREAM_FULL,
    STREAM_SUB,
    SUB_CHAN_AVG,
    read_latest_frame,
)
from ..config import Settings
from ..store import ShardReader, Store
from ..util import iso, parse_iso

log = logging.getLogger("casm_monitor.web.snaps")

# Above this span the 10 s / 96-channel stream is used instead of the 60 s
# full-resolution one (the plan's "sub for > 2 d").
SUB_SPAN_S = 2 * 86400.0
DEFAULT_MAX_CELLS = 1_000_000
# Hard bounds on what a caller may ask for. The upper one keeps a single
# response bounded; the lower one is a real limit, not a suggestion -- a request
# for 200 cells gets at most 200 cells, it is never quietly raised.
MIN_MAX_CELLS = 100
MAX_MAX_CELLS = 2_000_000
N_ADC = 12
# store event kind -> the epoch kind the frontend draws ("eq" | "adc_gain" |
# "obs_restart", docs/api-snaps.md).
EPOCH_KINDS = {
    "obs_restart": "obs_restart",
    "eq_change": "eq",
    "snap_eq_epoch": "eq",
    "adc_gain_change": "adc_gain",
}
RES_LABELS = ((15.0, "10s"), (300.0, "60s"), (1800.0, "10min"), (float("inf"), "1h"))


def _time_arg(field: str, text: str | None) -> float | None:
    """ISO-8601 or a unix timestamp; anything else is a 400."""
    if text is None or text == "":
        return None
    try:
        return float(text)
    except ValueError:
        pass
    parsed = parse_iso(text)
    if parsed is None:
        raise HTTPException(status_code=400, detail=f"{field} is not ISO-8601 or a unix timestamp")
    return parsed


def _int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def station_label(row: dict[str, str]) -> str | None:
    """``N21E1``-style label from the layout's row/col columns."""
    row_label = (row.get("row") or "").strip()
    col_label = (row.get("col") or "").strip()
    if not row_label and not col_label:
        return None
    return f"{row_label}{col_label}"


def board_table(
    layout_path: Any = None,
    snap_map_path: Any = None,
) -> list[dict[str, Any]]:
    """One card per board: the four antenna boards plus the three PPS relays.

    Per ``docs/api-snaps.md`` an antenna board always carries exactly twelve
    inputs in ADC order (nulls where the layout has no row for that ADC), and a
    relay card carries no ``inputs`` key at all.
    """
    # Resolved per call (not as a default argument) so the paths stay
    # patchable and a re-pointed ``current`` symlink is picked up.
    layout = rowmap.read_layout(layout_path or rowmap.LAYOUT_CSV)
    by_board: dict[tuple[int | None, str], dict[int, dict[str, str]]] = {}
    for row in layout:
        adc = _int(row.get("adc"))
        if adc is None:
            continue
        key = (_int(row.get("snap")), (row.get("snap_ip") or "").strip())
        by_board.setdefault(key, {})[adc] = row

    boards: list[dict[str, Any]] = []
    for entry in rowmap.read_snap_map(snap_map_path or rowmap.SNAP_MAP_CSV):
        ip = (entry.get("snap_ip") or "").strip()
        feng_id = _int(entry.get("feng_id"))
        # Unconnected layout rows carry no snap_ip, so the board is matched on
        # the logical snap index first and on the ip only as a fallback.
        rows: dict[int, dict[str, str]] = {}
        for (snap_id, snap_ip), entries in by_board.items():
            if (feng_id is not None and snap_id == feng_id) or (snap_ip and snap_ip == ip):
                rows.update(entries)
        inputs = []
        for adc in range(N_ADC):
            row = rows.get(adc)
            if row is None:
                inputs.append(
                    {
                        "adc": adc,
                        "packet_idx": None,
                        "antenna": None,
                        "station": None,
                        "in_bf": False,
                        "functional": False,
                    }
                )
                continue
            functional = bool(_int(row.get("functional"), 0))
            inputs.append(
                {
                    "adc": adc,
                    "packet_idx": _int(row.get("packet_idx")),
                    "antenna": _int(row.get("antenna")) if functional else None,
                    "station": station_label(row),
                    "in_bf": bool(_int(row.get("include_in_beamforming"), 0)),
                    "functional": functional,
                }
            )
        boards.append(
            {
                "ip": ip,
                "feng_id": feng_id,
                "slot": (entry.get("slot") or "").strip() or None,
                "role": "antenna",
                "inputs": inputs,
            }
        )
    boards.sort(key=lambda b: (b["feng_id"] is None, b["feng_id"]))
    for ip in rowmap.RELAY_IPS:
        # No "inputs" key: the frontend reads its absence as "PPS-only card".
        boards.append({"ip": ip, "feng_id": None, "slot": None, "role": "relay"})
    return boards


def input_to_row(mapping: dict[int, dict[str, Any]]) -> dict[int, int]:
    """packet_idx -> Kafka row (the formula assignment, "mismatch" included --
    that status is a validation flag, not a rejection)."""
    return {int(v): int(k) for k, v in rowmap.assignment(mapping).items()}


def db_to_linear(values: np.ndarray) -> np.ndarray:
    """dB -> linear power, floored the same way ``kafka_bp.to_db`` floors it."""
    return np.maximum(np.power(10.0, np.asarray(values, dtype=np.float64) / 10.0), DB_FLOOR)


def linear_to_db(power: np.ndarray) -> np.ndarray:
    """Linear power -> dB, with the same floor."""
    return 10.0 * np.log10(np.maximum(np.asarray(power, dtype=np.float64), DB_FLOOR))


def average_db(values: np.ndarray, n: int) -> np.ndarray:
    """Average dB values over the last axis IN LINEAR POWER, back to dB.

    Averaging dB directly is a geometric mean: it under-reports a band that has
    one hot channel and buries a dead channel's contribution, so every
    decimation of a spectrum goes through the power domain.
    """
    return linear_to_db(average_channels(db_to_linear(values), n))


def average_db_axis0(values: np.ndarray, n: int) -> np.ndarray:
    """Same as :func:`average_db` over the FIRST axis (time)."""
    return linear_to_db(average_channels(db_to_linear(values).T, n).T)


def average_channels(values: np.ndarray, nchan: int) -> np.ndarray:
    """Block-average the last axis down to at most ``nchan`` points."""
    values = np.asarray(values, dtype=np.float64)
    n = values.shape[-1]
    if nchan <= 0 or nchan >= n:
        return values
    block = int(np.ceil(n / nchan))
    keep = (n // block) * block
    head = values[..., :keep].reshape(*values.shape[:-1], keep // block, block).mean(axis=-1)
    if keep == n:
        return head
    tail = values[..., keep:].mean(axis=-1, keepdims=True)
    return np.concatenate([head, tail], axis=-1)


def decimate(z: np.ndarray, times: np.ndarray, freq: np.ndarray, max_cells: int):
    """Reduce a (time, freq) dB matrix to at most ``max_cells`` cells.

    Both axes are block-AVERAGED in linear power (never strided, never averaged
    in dB), and ``len(t) * len(freq) <= max_cells`` holds strictly on the way
    out: a time-heavy matrix collapses frequency to a single channel rather than
    returning more cells than asked for.
    """
    nt, nf = z.shape
    if nt * nf <= max_cells or nt == 0 or nf == 0:
        return z, times, freq
    # Split the budget so neither axis collapses if it does not have to: keep
    # the aspect ratio.
    scale = np.sqrt((nt * nf) / float(max_cells))
    target_t = max(1, int(np.floor(nt / max(1.0, scale))))
    # Even at one channel the time axis may not exceed the budget.
    target_t = min(target_t, int(max_cells))
    if target_t < nt:
        z = average_db_axis0(z, target_t)
        times = average_channels(times, target_t)
    nt = z.shape[0]
    target_f = max(1, int(max_cells // max(1, nt)))
    if nf > target_f:
        z = average_db(z, target_f)
        freq = average_channels(freq, target_f)
    assert z.shape[0] * z.shape[1] <= max_cells
    return z, times, freq


def _snapread_available() -> bool:
    """True once the board-read router module exists in the package."""
    import importlib.util

    return importlib.util.find_spec("casm_monitor.web.snapread") is not None


def res_label(dt_s: float) -> str:
    """The contract's resolution vocabulary for a sample spacing."""
    for limit, label in RES_LABELS:
        if dt_s <= limit:
            return label
    return RES_LABELS[-1][1]


def _hold_last(times: list[float], values: list, axis: list[float]) -> list:
    """Sample a sparse series onto ``axis`` by holding the last value."""
    out: list = []
    i = 0
    current = None
    for t in axis:
        while i < len(times) and times[i] <= t:
            current = values[i]
            i += 1
        out.append(current)
    return out


def build_router(
    settings: Settings,
    reader: Store,
    *,
    board_read_stub: bool | None = None,
) -> APIRouter:
    """The SNAPs router. ``board_read_stub`` defaults to "only if the real
    board-read module is absent"."""
    router = APIRouter(prefix="/api/snaps", tags=["snaps"])
    shards = ShardReader(reader)

    def mapping_now() -> dict[int, dict[str, Any]]:
        # The unconditional formula assignment (row = 2 * packet_idx) for
        # every wired input, overlaid with the daily/obs-restart validation
        # verdict persisted in the store ("formula" | "formula+verified" |
        # "mismatch"); see casm_monitor.collectors.rowmap module docstring.
        return rowmap.current_mapping(reader)

    @router.get("/boards")
    def boards() -> dict[str, Any]:
        try:
            return {"boards": board_table()}
        except OSError as exc:
            raise HTTPException(status_code=503, detail=f"layout unreadable: {exc}") from exc

    @router.get("/live")
    def live(ip: str, nchan: int = Query(default=3072, ge=1, le=3072)) -> dict[str, Any]:
        table = {b["ip"]: b for b in board_table()}
        board = table.get(ip)
        if board is None:
            raise HTTPException(status_code=404, detail=f"no board with ip {ip}")
        frame = read_latest_frame(settings)
        mapping = mapping_now()
        by_input = input_to_row(mapping)
        freq = rowmap.freq_axis_mhz()
        freq_out = average_channels(freq, nchan)
        rows_index = {row: i for i, row in enumerate(frame["rows"])} if frame else {}

        inputs = []
        for item in board.get("inputs") or []:
            packet_idx = item.get("packet_idx")
            row = by_input.get(int(packet_idx)) if packet_idx is not None else None
            values = None
            if frame is not None and row is not None and row in rows_index:
                values = [
                    round(float(v), 3)
                    for v in average_db(frame["power_db"][rows_index[row]], nchan)
                ]
            status = mapping.get(row, {}).get("status", "formula") if row is not None else "unmapped"
            inputs.append(
                {
                    "adc": item.get("adc"),
                    "packet_idx": packet_idx,
                    "antenna": item.get("antenna"),
                    "station": item.get("station"),
                    "row": row,
                    "bp": values,
                    "mapping": status,
                }
            )
        ts = None if frame is None else frame["ts"]
        return {
            "ip": ip,
            "ts": iso(ts),
            "age_s": None if ts is None else round(max(0.0, time.time() - ts), 1),
            "freq_mhz": [round(float(f), 5) for f in freq_out],
            "subbands_ok": None if frame is None else frame["subbands_ok"],
            "inputs": inputs,
            "eq_epoch": None,
            "units": "dB",
        }

    @router.get("/history")
    def history(
        packet_idx: int,
        t0: str | None = None,
        t1: str | None = None,
        source: str = "kafka",
        max_cells: int = DEFAULT_MAX_CELLS,
    ) -> dict[str, Any]:
        if source != "kafka":
            raise HTTPException(
                status_code=501, detail="only source=kafka exists until the board-read half lands"
            )
        max_cells = max(MIN_MAX_CELLS, min(int(max_cells), MAX_MAX_CELLS))
        now = time.time()
        start = _time_arg("t0", t0)
        end = _time_arg("t1", t1)
        end = now if end is None else end
        start = end - 3600.0 if start is None else start
        if end <= start:
            raise HTTPException(status_code=400, detail="t1 must be after t0")

        mapping = mapping_now()
        row = input_to_row(mapping).get(int(packet_idx))
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"input {packet_idx} is not mapped to a Kafka row (mapping unresolved)",
            )
        stream = STREAM_SUB if (end - start) > SUB_SPAN_S else STREAM_FULL
        chan_avg = SUB_CHAN_AVG if stream == STREAM_SUB else 1
        z_rows: list[np.ndarray] = []
        t_rows: list[float] = []
        for shard in shards.list(stream, t0=start, t1=end):
            meta = shard["meta"] or {}
            rows = [int(r) for r in (meta.get("rows") or [])]
            if row not in rows:
                continue
            array, _meta = shards.load(shard)
            times = np.asarray(meta.get("t") or [], dtype=np.float64)
            if times.size != array.shape[0]:
                times = np.linspace(shard["t0"], shard["t1"], array.shape[0])
            keep = (times >= start) & (times <= end)
            if not keep.any():
                continue
            z_rows.append(np.asarray(array[keep][:, rows.index(row), :], dtype=np.float32))
            t_rows.extend(float(v) for v in times[keep])
        if not z_rows:
            return {
                "packet_idx": packet_idx,
                "row": row,
                "t": [],
                "freq_mhz": [],
                "z_db": [],
                "res": res_label(float(chan_avg) * FRAME_CADENCE_S),
                "stream": stream,
                "units": "dB",
            }
        z = np.concatenate(z_rows, axis=0)
        times = np.asarray(t_rows, dtype=np.float64)
        order = np.argsort(times)
        z, times = z[order], times[order]
        freq = average_channels(rowmap.freq_axis_mhz(), z.shape[1])
        n_t_raw = z.shape[0]
        z, times, freq = decimate(z, times, freq, max_cells)
        dt = float(np.median(np.diff(times))) if times.size > 1 else FRAME_CADENCE_S
        return {
            "packet_idx": packet_idx,
            "row": row,
            # unix seconds, per docs/api-snaps.md; t_iso is an extra convenience
            "t": [float(v) for v in times],
            "t_iso": [iso(float(v)) for v in times],
            "freq_mhz": [round(float(f), 5) for f in freq],
            "z_db": [[round(float(v), 2) for v in line] for line in z],
            "res": res_label(dt),
            "stream": stream,
            "n_samples_raw": n_t_raw,
            "units": "dB",
        }

    @router.get("/trend")
    def trend(packet_idx: int, t0: str | None = None, t1: str | None = None) -> dict[str, Any]:
        now = time.time()
        end = _time_arg("t1", t1) or now
        start = _time_arg("t0", t0)
        start = end - 30 * 86400.0 if start is None else start

        def tagged_series(name_sql: str, args: list[Any]) -> tuple[list[float], list[Any]]:
            rows = reader.query(
                "SELECT ts, value FROM scalars WHERE " + name_sql +
                " AND json_extract(tags, '$.packet_idx') = ? AND ts >= ? AND ts <= ? "
                "ORDER BY ts ASC",
                [*args, int(packet_idx), start, end],
            )
            return [float(r["ts"]) for r in rows], [r["value"] for r in rows]

        night_t, night_v = tagged_series("name = ?", ["snaps.night_median_db"])
        band_t, band_v = tagged_series(
            "name LIKE ? AND name LIKE ?", ["kafka_bp.row%", "%.power_db"]
        )
        # One shared axis (the contract's single ``t``): the dense band-power
        # samples, with the daily night median held until the next night.
        times = band_t or night_t
        night_on_axis = _hold_last(night_t, night_v, times)
        band_on_axis = band_v if band_t else [None] * len(times)
        epochs = []
        for kind, label_kind in EPOCH_KINDS.items():
            for event in reader.events(kind=kind, since=start, limit=500):
                if event["ts"] <= end:
                    epochs.append(
                        {
                            "ts": iso(event["ts"]),
                            "kind": label_kind,
                            "label": event["subject"] or kind,
                        }
                    )
        epochs.sort(key=lambda item: item["ts"] or "")
        return {
            "packet_idx": packet_idx,
            "t": times,
            "t_iso": [iso(v) for v in times],
            "night_median_db": night_on_axis,
            "band_power_db": band_on_axis,
            "night_t": night_t,
            "night_values": night_v,
            "epochs": epochs,
        }

    # -- board reads (owned by the snapread half of M1) ------------------
    # TODO(m1-snapread): these two stubs exist only so the frontend gets a
    # clean 501 instead of a 404 until casm_monitor/web/snapread.py (the
    # board-read router plus the ``snap_read`` job kind) is mounted. They are
    # not registered once that module exists, so they can never shadow the real
    # routes whichever order the routers are included in.
    if board_read_stub if board_read_stub is not None else not _snapread_available():

        @router.get("/board-read")
        def board_read_get(ip: str | None = None) -> dict[str, Any]:
            raise HTTPException(
                status_code=501,
                detail="board reads are not implemented yet (M1 snapread half)",
            )

        @router.post("/board-read")
        def board_read_post(body: dict[str, Any] | None = None) -> dict[str, Any]:
            raise HTTPException(
                status_code=501,
                detail="board reads are not implemented yet (M1 snapread half)",
            )

    @router.get("/mapping")
    def mapping_route() -> dict[str, Any]:
        """The row -> input mapping: the formula assignment for every wired
        input, plus the last validation's correlation scores where one has
        run (``ts``/``source`` are ``null`` for a row never validated)."""
        mapping = mapping_now()
        return {
            "mapping": {
                str(row): {
                    "packet_idx": item.get("packet_idx"),
                    "corr": item.get("corr"),
                    "runner_up": item.get("runner_up"),
                    "status": item.get("status"),
                    "ts": iso(item["ts"]) if item.get("ts") is not None else None,
                }
                for row, item in sorted(mapping.items())
            },
            "source": next(
                (item.get("source", {}) for item in mapping.values() if item.get("source")), {}
            ),
        }

    return router


__all__ = [
    "build_router",
    "board_table",
    "decimate",
    "average_channels",
    "average_db",
    "station_label",
]
