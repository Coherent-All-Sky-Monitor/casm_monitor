"""Date-based defaults and provenance for a Calibration-tab build.

The workspace's initial date/windows instead come from the uploaded recipe
(``web.calibration_workspace.workspace_defaults``). This module supplies the
date-based fallback and the defaults when choosing a different day:

* **solve window** = the source's altitude maximum on the chosen UTC date,
  found with :func:`casm_vis_analysis.sources.source_altaz` on a 1-minute grid,
  plus/minus ``cal.window_half_min``. The maximum's own time and altitude are
  reported next to the window, so the operator sees what the window is centred
  on instead of a bare pair of timestamps (plan.md: "offset shown explicitly").
  The 2026-09-04 deployed product used exactly this recipe: "Sun cal window
  2026-09-03 19:22-20:22 UTC centred on the altitude maximum"
  (``deployed_weights.csv`` last row).
* **static window** = ``cal.static_default`` (03:00-03:30) on the SAME UTC date,
  which is the local night BEFORE the transit. That is the window the
  2026-08-31 and 2026-09-04 products used. It is a default, not a standing
  truth: the static carries antenna amplitudes, so any EQ/gain change since it
  was recorded invalidates it, and casm-wiki ``weights-verification.md`` wants
  the quiet window re-derived per epoch (``source_altaz`` over the candidates)
  rather than copied forward.
* **antenna set** = the slots actually POPULATED in the deployed CB weights
  file (``antennas_source: "deployed"``), read with
  :func:`deployed_cb_antennas`. This is what is live, which is not always the
  layout's ``include_in_beamforming=1`` column: that column can be edited
  after the last build without a rebuild (2026-09-09 finding: layout gate =
  18 antennas, adds 12 and 33, lacks 18; deployed product = the 17-antenna
  set). The layout gate set is exposed separately as ``layout_bf_antennas``
  with a note when the two differ, and the weights stage still silently
  intersects any REQUESTED antenna list with the layout column (casm-wiki
  incidents.md 2026-08-31), so a build only ever gets fewer populated slots
  than requested, never more. Falls back to ``layout_bf_antennas``
  (``antennas_source: "layout_fallback"``) when the deployed CB file cannot be
  read.
* **ref ant** = 9 when it is in the set (the reference every deployed cal since
  Aug-15 used), else the lowest antenna in the set.
* **deployed product** = six agreeing registry stream associations. Successful
  monitor upload receipts bind calibration paths and header scales, including
  saved-default reloads. Legacy values come from ``deployed_weights.csv`` with
  explicit source labels. Neither establishes fresh GPU activation.

Read-only: this module opens the layout CSV, the ledger and the registry for
reading and writes nothing.
"""

from __future__ import annotations

import csv
import hashlib
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import Settings
from .collectors.weights import (
    load_product,
    read_last_ledger_row,
)

#: Sources the Calibration tab offers. Only the ones in
#: ``settings.cal_sources_enabled`` may be built on; the rest are listed as
#: disabled ("not yet": the array is not sensitive enough to solve on them
#: today, operator 2026-09-08).
KNOWN_SOURCES: tuple[str, ...] = ("sun", "cyg-a", "cas-a", "tau-a", "vir-a")

#: Reference antenna of every deployed cal since 2026-08-15, used when it is in
#: the requested set.
PREFERRED_REF_ANT = 9

#: Grid step of the altitude search, seconds. One minute is the same sampling
#: the driver's own track/transit helpers use.
ALT_GRID_S = 60.0

#: The driver's own spelling of a timestamp (``RecipeParams.source_window``).
UTC_FMT = "%Y-%m-%d %H:%M:%S"


def parse_date(date: str) -> datetime:
    """``YYYY-MM-DD`` -> midnight UTC of that date."""
    return datetime.strptime(str(date).strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _utc(ts: float) -> str:
    """Unix seconds -> ISO-8601 Z, the wire format of every API timestamp.

    The driver wants ``"YYYY-MM-DD HH:MM:SS"``; the build validator accepts
    either and normalises to that, so the tab speaks one format end to end
    (docs/api-cal.md).
    """
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def altitude_maximum(source: str, date: str, step_s: float = ALT_GRID_S) -> dict[str, Any]:
    """When the source is highest on ``date`` (UTC), on a ``step_s`` grid.

    Returns the maximum's unix time, its UTC string and its altitude. The Sun's
    maximum is not local noon and moves through the year, so it is measured
    every time rather than tabulated.
    """
    import numpy as np
    from casm_vis_analysis.sources import source_altaz

    t0 = parse_date(date).timestamp()
    grid = np.arange(t0, t0 + 86400.0, float(step_s))
    alt, _az = source_altaz(source.lower().replace("-", "_").replace("+", "_"), grid)
    alt = np.asarray(alt, dtype=float)
    k = int(np.nanargmax(alt))
    return {
        "t_unix": float(grid[k]),
        "utc": _utc(float(grid[k])),
        "alt_deg": round(float(alt[k]), 3),
        "grid_step_s": float(step_s),
    }


def solve_window(source: str, date: str, half_min: float) -> dict[str, Any]:
    """Solve window centred on the altitude maximum, with the maximum reported."""
    peak = altitude_maximum(source, date)
    half_s = float(half_min) * 60.0
    return {
        "window": [_utc(peak["t_unix"] - half_s), _utc(peak["t_unix"] + half_s)],
        "alt_max_utc": peak["utc"],
        "alt_max_unix": peak["t_unix"],
        "alt_max_deg": peak["alt_deg"],
        "half_width_min": float(half_min),
        # The window is centred ON the maximum: stated rather than implied, so
        # a future offset default is visible in the payload instead of hidden
        # in the timestamps.
        "center_offset_min": 0.0,
        "grid_step_s": peak["grid_step_s"],
    }


def static_window(date: str, spec: str) -> list[str] | None:
    """``"03:00-03:30"`` on ``date`` -> the pair of UTC strings, or None.

    An empty/``none`` spec means "no static", which is a legitimate build (the
    no-static leg is what the wiki prescribes when no matched static exists
    yet, weights-verification.md).
    """
    text = str(spec or "").strip()
    if not text or text.lower() in {"none", "null", "off"}:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", text)
    if not m:
        raise ValueError(f"static window {spec!r} is not HH:MM-HH:MM")
    day = parse_date(date)
    h0, m0, h1, m1 = (int(x) for x in m.groups())
    start = day + timedelta(hours=h0, minutes=m0)
    end = day + timedelta(hours=h1, minutes=m1)
    if end <= start:  # a window that wraps past midnight ends on the next day
        end += timedelta(days=1)
    return [_utc(start.timestamp()), _utc(end.timestamp())]


def sha256_file(path: str | Path) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def layout_info(path: str | Path) -> dict[str, Any]:
    """Provenance of the antenna layout: resolved path, sha256 and both sets.

    ``sha256`` is of the RESOLVED file (``current`` is a symlink), which is what
    ``deploy_upload`` compares the build's snapshot against: repointing the
    symlink between build and upload changes it.
    """
    p = Path(path)
    resolved = p.resolve()
    bf: list[int] = []
    wired: list[int] = []
    if resolved.is_file():
        with resolved.open(newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    ant = int(str(row.get("antenna") or "").strip())
                except ValueError:
                    continue
                if str(row.get("functional") or "").strip() == "1":
                    wired.append(ant)
                if str(row.get("include_in_beamforming") or "").strip() == "1":
                    bf.append(ant)
    return {
        "path": str(p),
        "resolved": str(resolved),
        "sha256": sha256_file(resolved),
        "antennas": sorted(bf),
        "wired_antennas": sorted(wired),
        "n_bf": len(bf),
        "n_wired": len(wired),
    }


#: Retained legacy channel constant; deployed membership now scans all channels.
DEPLOYED_CB_CHECK_CHAN = 1500


@lru_cache(maxsize=4)
def _populated_antennas(path, size, mtime_ns):
    import h5py
    import numpy as np
    with h5py.File(path, 'r') as f:
        ids = np.asarray(f['array_config/antenna_ids'][:], dtype=int)
        weights = f['weights_int8']
        if weights.ndim != 5 or weights.shape[0] != 2 or weights.shape[-1] > len(ids):
            raise ValueError('Weights slot metadata disagrees with payload')
        if weights.size * weights.dtype.itemsize > 400 * 1024**2:
            raise ValueError('Weights exceed the 400 MiB inspection budget')
        populated = np.zeros(weights.shape[-1], dtype=bool)
        for channel in range(0, weights.shape[1], 64):
            populated |= np.any(weights[:, channel:channel+64] != 0, axis=(0,1,2,3))
        ants = tuple(sorted(int(a) for a in ids[:len(populated)][populated]))
        if any(a <= 0 for a in ants):
            raise ValueError('Populated slots have no valid antenna identity')
    after = Path(path).stat()
    if (after.st_size, after.st_mtime_ns) != (size, mtime_ns):
        raise ValueError('Weights changed during inspection')
    return ants


def deployed_cb_antennas(weights_h5: str | Path | None) -> dict[str, Any]:
    """Antennas actually populated in the DEPLOYED CB weights file.

    Reads ``array_config/antenna_ids`` (the saved slot-to-antenna map) and
    nonzero ``weights_int8`` slots over every channel, polarization and beam.
    Results are cached by file identity with a 400 MiB inspection budget.
    This is what the recorded product contains,
    which is not always the layout's ``include_in_beamforming`` column (that
    column can be edited after the last build without a rebuild — 2026-09-09
    finding: the layout gate lists 18 antennas, adds 12 and 33, lacks 18,
    while the deployed product is the 17-antenna set).

    Returns ``{"antennas": [...], "error": None}`` or ``{"antennas": [],
    "error": "..."}`` when the file is missing or unreadable; callers fall
    back to the layout set in that case, never guess.
    """
    result: dict[str, Any] = {"antennas": [], "error": None, "path": str(weights_h5) if weights_h5 else None}
    if not weights_h5:
        result["error"] = "no deployed weights file on record"
        return result
    path = Path(weights_h5)
    if not path.is_file():
        result["error"] = f"deployed weights file {weights_h5} does not exist"
        return result
    try:
        stat = path.stat()
        result['antennas'] = list(_populated_antennas(str(path.resolve()), stat.st_size, stat.st_mtime_ns))
    except Exception as exc:  # noqa: BLE001 - surfaced as a note, not fatal
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def default_ref_ant(antennas: list[int]) -> int | None:
    if not antennas:
        return None
    return PREFERRED_REF_ANT if PREFERRED_REF_ANT in antennas else min(antennas)


def build_tag(date: str, when_utc: str) -> str:
    """``cal_<YYYYMMDD>_<HHMM>``, the HHMM being the altitude maximum's."""
    hhmm = when_utc[11:16].replace(":", "") if len(when_utc) >= 16 else "0000"
    return f"cal_{str(date).replace('-', '')}_{hhmm}"


# ---------------------------------------------------------------------------
# The deployed product: registry + successful receipts, legacy ledger fallback
# ---------------------------------------------------------------------------

def _first_path(field: str | None) -> str | None:
    """First path in a ledger cell (they carry '(+ ib_...)' / prose suffixes)."""
    text = (field or "").strip()
    if not text:
        return None
    head = text.split("(")[0].strip()
    return head or None


def _ib_path(field: str | None) -> str | None:
    """The '(+ ib_xxx.h5)' companion of a ledger weights cell, resolved.

    The ledger names the IB mask relative to the CB file's directory, e.g.
    ``.../weights_b0329_20260904_17ant_512_int8.h5 (+ ib_b0329_20260904_17ant.h5)``.
    """
    text = (field or "").strip()
    m = re.search(r"\(\+\s*([^)\s]+\.h5)\s*\)", text)
    if not m:
        return None
    ib = m.group(1)
    if ib.startswith("/"):
        return ib
    cb = _first_path(text)
    return str(Path(cb).parent / ib) if cb else ib


def parse_scale_pairing(row: dict[str, str] | None) -> dict[str, Any]:
    """CB/IB SCALE pairing of the ledger row. Raises if it cannot be read.

    The pairing is written in the row's prose ("--scale 8064 --ib-scale 32
    (Route Z pairing)"), so it is parsed, never assumed: hardcoding it is what
    the hard rules forbid, and the deployed value is currently NOT the
    documented 32/32 default.
    """
    text = " ".join(str(v) for v in (row or {}).values())
    scale = re.search(r"--scale[= ]+(\d+)", text)
    ib = re.search(r"--ib-scale[= ]+(\d+)", text)
    if scale is None or ib is None:
        raise ValueError(
            "could not read the CB/IB SCALE pairing from the deployed_weights.csv "
            "last row (expected '--scale N --ib-scale M' in its text); refusing "
            "to guess. Fix the ledger row, then re-stage."
        )
    return {
        "scale": int(scale.group(1)),
        "ib_scale": int(ib.group(1)),
        "source": "deployed_weights.csv last row",
        "date_deployed": (row or {}).get("date_deployed"),
    }


def deployed_product(settings: Settings) -> dict[str, Any]:
    """Recorded deployment: six agreeing streams, with a bound cal reference.

    The ledger remains a legacy reference, not a veto on a newer deployment.
    A consistent registry product and successful receipt bind a saved calibration;
    a different/unidentified product never inherits the previous calibration.
    """
    from .observation import latest_stream_events
    from .deployment_metadata import successful_uploads, digest
    from .util import parse_iso
    row = read_last_ledger_row(settings.deployed_weights_csv)
    events = latest_stream_events(Path(settings.registry_dir) / "live_events.jsonl")
    product_id = None
    product = None
    ids = {e.get('product_id') for e in events}
    consistent = len(events) == 6 and len(ids) == 1 and None not in ids
    if consistent:
        product_id = next(iter(ids))
        product = load_product(settings.registry_dir, product_id)
    weights_file = product.get('h5_path') if product else None
    ledger_weights = _first_path((row or {}).get('weights_file'))
    ledger_matches = bool(weights_file and ledger_weights and
                          Path(weights_file).resolve() == Path(ledger_weights).resolve())
    legacy = not events
    cal_file = _first_path((row or {}).get('cal_file')) if ledger_matches or legacy else None
    reference_error = None
    try:
        pairing: dict[str, Any] | None = parse_scale_pairing(row)
        pairing_error = None
    except ValueError as exc:
        pairing, pairing_error = None, str(exc)
    receipts = successful_uploads(settings)
    # Product metadata is first-write-only: SCALE changes do not change payload
    # hashes. Bind CB receipts to the current per-stream upload timestamps.
    cb_receipts = []
    def matches_load(receipt, event, stamp):
        if receipt['kind'] not in ('cb', 'both') or event['stream'] not in receipt['streams']:
            return False
        if receipt['product_id'] != event['product_id']:
            return False
        if event.get('source') == 'defaults':
            return receipt['save_defaults'] and receipt['completed'] <= stamp
        return (event.get('source') == 'upload'
                and receipt['ts'] - 1 <= stamp <= receipt['completed'] + 1)
    for event in events:
        stamp = parse_iso(event['utc'])
        receipt = next((r for r in receipts if matches_load(r, event, stamp)), None)
        cb_receipts.append(receipt)
    cb_values = [r['scale'] if r else (pairing['scale'] if pairing and ledger_matches else None)
                 for r in cb_receipts]
    cb_scale = (cb_values[0] if len(cb_values) == 6 and None not in cb_values and len(set(cb_values)) == 1
                else pairing['scale'] if legacy and pairing else None)
    reference_source = 'ledger' if cal_file else 'unavailable'
    deployed_date = (row or {}).get('date_deployed') if ledger_matches or legacy else None
    if consistent and any(cb_receipts):
        receipt = max((r for r in cb_receipts if r), key=lambda r: r['ts'])
        deployed_date = _utc(receipt['ts'])
        # A new export alone never becomes the current reference; this path is
        # reached only through a successful upload bound to live associations.
        cal_file = None
        try:
            candidate = receipt.get('cal_h5')
            if not candidate or not receipt.get('cal_sha256') or digest(candidate) != receipt['cal_sha256']:
                raise ValueError('Saved upload calibration is missing or has changed')
            cal_file = candidate
            reference_source = 'successful_upload'
        except (OSError, ValueError) as exc:
            reference_error = str(exc)
    # IB has no CB-pointing registry. Its last successful per-stream receipt is
    # explicit recorded evidence, not a claim to have re-read node RAM/defaults.
    ib_receipts = [next((r for r in receipts if r['kind'] in ('ib', 'both')
                         and stream in r['streams']), None) for stream in range(6)]
    ib_values = [r['ib_scale'] if r else pairing['ib_scale'] if pairing else None for r in ib_receipts]
    ib_paths = [r['ib_h5'] if r else _ib_path((row or {}).get('weights_file')) for r in ib_receipts]
    ib_scale = ib_values[0] if None not in ib_values and len(set(ib_values)) == 1 else None
    ib_file = ib_paths[0] if None not in ib_paths and len(set(ib_paths)) == 1 else None
    if cb_scale is None or ib_scale is None:
        pairing_error = 'No single recorded SCALE pairing across all six streams; review the deployment records.'
    return {
        "cal_file": cal_file,
        "weights_file": weights_file or (ledger_weights if legacy else None),
        "ib_file": ib_file,
        "date_deployed": deployed_date,
        "last_load_utc": max(e['utc'] for e in events) if product else None,
        "n_ant_set": (row or {}).get("n_ant_set") if ledger_matches or legacy else None,
        "scale": cb_scale,
        "ib_scale": ib_scale,
        "scale_error": pairing_error,
        "product_id": product_id,
        "ledger_row": dict(row) if row else None,
        "reference_error": reference_error,
        "reference_source": reference_source,
        "scale_source": 'successful_upload_and_ledger' if any(cb_receipts) or any(ib_receipts) else 'ledger',
        "source": 'live_event' if product else 'ledger_only' if legacy else 'unknown',
        "ledger_matches": ledger_matches,
    }


# ---------------------------------------------------------------------------
# The tab's defaults
# ---------------------------------------------------------------------------

def source_list(settings: Settings) -> list[dict[str, Any]]:
    enabled = {s.lower() for s in settings.cal_sources_enabled}
    names = list(KNOWN_SOURCES) + [s for s in sorted(enabled) if s not in KNOWN_SOURCES]
    return [{"name": n, "enabled": n in enabled} for n in names]


def cal_defaults(date: str, settings: Settings | None = None) -> dict[str, Any]:
    """Everything the Calibration form pre-fills for ``date`` (UTC).

    ``date`` is the UTC date of the SOLVE; the static default sits on the same
    UTC date at 03:00 (the local night before it).
    """
    from .config import load_settings

    settings = settings or load_settings()
    source = (settings.cal_sources_enabled or ("sun",))[0]
    window = solve_window(source, date, settings.cal_window_half_min)
    layout = layout_info(settings.layout_csv)
    layout_bf_antennas = layout["antennas"]
    deployed = deployed_product(settings)
    deployed_cb = deployed_cb_antennas(deployed.get("weights_file"))
    deployed_antennas = deployed_cb["antennas"]
    if deployed_antennas:
        antennas = deployed_antennas
        antennas_source = "deployed"
    else:
        # No readable deployed CB file (fresh store, bad path, ...): fall
        # back to the layout gate rather than defaulting to an empty form.
        antennas = layout_bf_antennas
        antennas_source = "layout_fallback"
    layout_mismatch = sorted(set(layout_bf_antennas) ^ set(deployed_antennas)) if deployed_antennas else []
    return {
        "date": str(date),
        "source": source,
        "sources": source_list(settings),
        "source_window": window["window"],
        "alt_max_utc": window["alt_max_utc"],
        # docs/api-cal.md's names for the same two numbers.
        "sun_max_utc": window["alt_max_utc"],
        "window_offset_min": window["half_width_min"],
        "alt_max_deg": window["alt_max_deg"],
        "window_half_min": window["half_width_min"],
        "window_center_offset_min": window["center_offset_min"],
        "static_window": static_window(date, settings.cal_static_default),
        "static_default": settings.cal_static_default,
        "static_note": (
            "03:00-03:30 UTC on the solve date (the local night before) is the "
            "window the 2026-08-31 and 2026-09-04 products used. Re-derive it "
            "for this epoch before trusting it: a static recorded across an EQ "
            "or gain change carries the wrong amplitudes "
            "(casm-wiki weights-verification.md)."
        ),
        "antennas": antennas,
        "n_ant": len(antennas),
        "antennas_source": antennas_source,
        "antennas_note": (
            f"{len(antennas)} antennas populated in the deployed CB weights file "
            f"{deployed.get('weights_file')}"
            if antennas_source == "deployed"
            else (
                f"deployed CB weights unreadable ({deployed_cb['error']}); falling "
                f"back to the {len(antennas)} antennas with include_in_beamforming=1 "
                f"in {layout['path']}"
            )
        ),
        "layout_bf_antennas": layout_bf_antennas,
        "deployed_antennas": deployed_antennas,
        "antennas_mismatch_note": (
            None
            if not layout_mismatch
            else (
                f"the layout's include_in_beamforming set and the deployed CB "
                f"weights' antenna set differ: {layout_mismatch} "
                f"(2026-09-09 finding: the layout gate can drift from what was "
                f"last built without a rebuild)"
            )
        ),
        "ref_ant": default_ref_ant(antennas),
        "grid_mode": "exact",
        "n_beams": 512,
        "diagnostics": True,
        "notebook": True,
        "execute_notebook": True,
        "prev_cal_path": deployed.get("cal_file"),
        "tag": build_tag(date, window["alt_max_utc"]),
        "layout": {
            "path": layout["path"],
            "resolved": layout["resolved"],
            "sha256": layout["sha256"],
            "n_bf": layout["n_bf"],
            "n_wired": layout["n_wired"],
        },
        "deployed": {
            "cal_file": deployed.get("cal_file"),
            "weights_file": deployed.get("weights_file"),
            "ib_file": deployed.get("ib_file"),
            "scale": deployed.get("scale"),
            "ib_scale": deployed.get("ib_scale"),
            "scale_error": deployed.get("scale_error"),
            "product_id": deployed.get("product_id"),
            "date_deployed": deployed.get("date_deployed"),
        },
    }
