"""SNAP board inventory read from the layout repo, never duplicated in config.

Two CSVs, both owned by ``antenna_layouts`` and only ever read:

* ``casm_snap_map.csv`` — ``chassis,slot,feng_id,snap_ip`` for the antenna
  boards (feng_id 0..3 = .52 .51 .62 .73 today). The relay boards (.59 .68 .69)
  have no antennas and no feng_id and are listed in the config instead.
* the ``current`` symlink (``settings.snap_layout_csv``, config key
  ``snap.layout_csv``) — ``antenna,snap,adc,packet_idx,functional,...``, which
  gives every board input its correlator input index. This is the SAME file
  :func:`casm_monitor.web.snaps.board_table` reads (via
  ``casm_monitor.collectors.rowmap.LAYOUT_CSV``); :func:`read_layout_inputs`
  below is built on :func:`casm_monitor.collectors.rowmap.read_layout` so there
  is exactly one CSV-parsing routine between the snap_read job and the SNAPs
  API. When the file cannot be read we fall back to
  ``packet_idx = feng_id * 12 + adc`` (what the correlator's own indexing
  does) and say so through ``mapping``.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings

N_INPUTS = 12


@dataclass(frozen=True)
class Board:
    """One SNAP board: an antenna board with a feng_id, or a PPS-only relay."""

    ip: str
    role: str  # "antenna" | "relay"
    feng_id: int | None = None
    slot: str | None = None
    chassis: str | None = None


def read_snap_map(csv_path: str | Path) -> list[Board]:
    """Antenna boards from ``casm_snap_map.csv`` (empty list if unreadable)."""
    path = Path(csv_path)
    if not path.is_file():
        return []
    boards: list[Board] = []
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            ip = (row.get("snap_ip") or "").strip()
            if not ip:
                continue
            try:
                feng_id: int | None = int(row["feng_id"])
            except (KeyError, TypeError, ValueError):
                feng_id = None
            boards.append(
                Board(
                    ip=ip,
                    role="antenna",
                    feng_id=feng_id,
                    slot=(row.get("slot") or "").strip() or None,
                    chassis=(row.get("chassis") or "").strip() or None,
                )
            )
    boards.sort(key=lambda b: (b.feng_id is None, b.feng_id if b.feng_id is not None else 0))
    return boards


def antenna_boards(settings: Settings) -> list[Board]:
    """Antenna boards: the config override if given, else the SNAP map CSV."""
    if settings.snap_antenna_boards:
        return [Board(ip=str(ip), role="antenna") for ip in settings.snap_antenna_boards]
    return read_snap_map(settings.snap_map_csv)


def relay_boards(settings: Settings) -> list[Board]:
    return [Board(ip=str(ip), role="relay") for ip in settings.snap_relay_boards]


def all_boards(settings: Settings) -> list[Board]:
    """Antenna boards first (feng_id order), then the relays."""
    return antenna_boards(settings) + relay_boards(settings)


def board_ips(settings: Settings) -> list[str]:
    return [b.ip for b in all_boards(settings)]


def board_for_ip(settings: Settings, ip: str) -> Board | None:
    for board in all_boards(settings):
        if board.ip == ip:
            return board
    return None


def _read_layout_rows(csv_path: str | Path) -> list[dict[str, str]]:
    """The layout CSV as plain dicts, via the one CSV-parsing routine shared
    with :func:`casm_monitor.web.snaps.board_table`
    (:func:`casm_monitor.collectors.rowmap.read_layout`). Imported lazily to
    avoid a module-import cycle (``collectors`` imports ``collectors.snapread``
    imports this module at package-init time)."""
    from .collectors import rowmap

    path = Path(csv_path)
    if not path.is_file():
        return []
    return rowmap.read_layout(path)


def read_layout_inputs(csv_path: str | Path) -> dict[tuple[int, int], dict[str, Any]]:
    """``(feng_id, adc) -> {packet_idx, antenna, functional}`` from the layout.

    Reads the same file (and the same raw rows) as ``board_table()``'s layout
    grouping; only the indexing (by ``(snap, adc)`` rather than by board ip)
    differs, because the snap_read job addresses inputs by feng_id/adc.
    """
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for row in _read_layout_rows(csv_path):
        try:
            snap_id = int(row.get("snap", row.get("snap_id", "")))
            adc = int(row["adc"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            packet_idx = int(row.get("packet_idx", row.get("packet_index", "")))
        except (TypeError, ValueError):
            packet_idx = snap_id * N_INPUTS + adc
        try:
            antenna: int | None = int(row.get("antenna", row.get("antenna_id", "")))
        except (TypeError, ValueError):
            antenna = None
        functional = str(row.get("functional", "")).strip() in ("1", "true", "True")
        out[(snap_id, adc)] = {
            "packet_idx": packet_idx,
            "antenna": antenna,
            "functional": functional,
        }
    return out


def input_tags(
    layout: dict[tuple[int, int], dict[str, Any]], feng_id: int | None, adc: int
) -> dict[str, Any]:
    """Tags for one board input: packet_idx (mapped or derived) and antenna."""
    if feng_id is None:
        return {"adc": adc, "packet_idx": None, "antenna": None, "mapping": "unmapped"}
    entry = layout.get((feng_id, adc))
    if entry is None:
        # Not in the layout (unwired channel): the correlator input index is
        # still defined by the board and the ADC, so report it as derived.
        return {
            "adc": adc,
            "packet_idx": feng_id * N_INPUTS + adc,
            "antenna": None,
            "mapping": "derived",
        }
    return {
        "adc": adc,
        "packet_idx": entry["packet_idx"],
        "antenna": entry["antenna"],
        "mapping": "layout",
    }
