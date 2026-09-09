"""Kafka bandpass row -> correlator input mapping.

The producers publish ``nsig x npol`` signal rows per record and nothing in the
message says which correlator input a row is. The source formula
(``BlockFormatAntenna.cpp:148-154``) does not reproduce the deployed layout,
but the measured one does (``docs/notes/kafka-bandpass-schema.md``, 2026-09-08):

    row = 2 * packet_idx   (isig = packet_idx, pol 0; odd/pol-1 rows are
                             never populated in the deployed configuration)

:func:`formula_row`/:func:`formula_mapping` are this PRIMARY mapping: every
wired input gets a row unconditionally, with no correlation, no vis file and
no acceptance gate involved. The shape-correlation machinery below
(:func:`validate_rows`/:func:`validate_row_map`) is now a VALIDATION that runs
daily and on every ``obs_restart`` (``KafkaBandpassCollector._maybe_row_map``):
it cross-correlates each formula row's measured bandpass shape against the
autocorrelation of its own wired input (read with casm_io from the newest
COMPLETE visibility file) and flags the row ``"mismatch"`` if some OTHER
input's autocorrelation is a better match -- a red flag for the operator, not
a fallback to "unmapped". A row that has never been validated (or the
validation was inapplicable, e.g. the vis file was unavailable) reports
``"formula"``; one that has been checked and agrees reports
``"formula+verified"``.

Everything here is read-only outside the store: the layout CSVs and the
visibility files are opened for reading, never written.

Shape metric used by the validation (checked live on 2026-09-08: every wired
input's argmax already lands on itself, i.e. 24/24 verified):

1. band-limit to 400-480 MHz (the part of the band with real signal),
2. log10 of the power, minus its own median (a per-input gain is not an
   identity),
3. subtract the common bandpass -- the median shape of the ``COMMON_MODE_N``
   strongest spectra on each side. Without this step every input correlates
   with every other above 0.9; with it the correct assignment wins by
   0.1-0.5. There is no longer an acceptance floor/margin that blocks the
   mapping -- the scores are kept only for ``/api/snaps/mapping``.

Then Pearson correlation over the band, argmax per row.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

from ..store import Store

log = logging.getLogger("casm_monitor.collect.rowmap")

# Correlator frequency axis (layout_64ant, descending). The Kafka records carry
# their own ``freq``/``bw`` headers, but those disagree with the correlator by
# ~0.3 MHz on subband 0, so the axis below (casm_io's) is the authority and the
# Kafka ``offset`` header only says which 512-channel block a record fills.
NCHAN = 3072
FREQ_TOP_MHZ = 484.375
CHAN_BW_MHZ = 93.75 / 3072.0

BAND_LO_MHZ = 400.0
BAND_HI_MHZ = 480.0
COMMON_MODE_N = 12

SNAP_MAP_CSV = Path("/home/casm/software/dev/antenna_layouts/casm_snap_map.csv")
LAYOUT_CSV = Path("/home/casm/software/dev/antenna_layouts/current")
# Boards with no antennas that only relay the PPS chain (wiki: snap-recovery).
RELAY_IPS = ("192.168.120.59", "192.168.120.68", "192.168.120.69")


def freq_axis_mhz(nchan: int = NCHAN) -> np.ndarray:
    """Descending channel frequencies, same convention as casm_io."""
    return FREQ_TOP_MHZ - np.arange(nchan, dtype=np.float64) * CHAN_BW_MHZ


# -- layout -------------------------------------------------------------
def read_layout(path: str | os.PathLike[str] = LAYOUT_CSV) -> list[dict[str, str]]:
    """The layout CSV as plain dicts (re-read on every call, it is small).

    ``antenna_layouts/current`` is a symlink, so ``casm-layout apply`` swapping
    it is picked up without a restart.
    """
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def read_snap_map(path: str | os.PathLike[str] = SNAP_MAP_CSV) -> list[dict[str, str]]:
    """chassis/slot/feng_id/snap_ip of the antenna boards."""
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def _int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def wired_inputs(layout: Iterable[dict[str, str]]) -> list[int]:
    """Sorted packet_idx of every wired input (``functional = 1``)."""
    out = set()
    for row in layout:
        idx = _int(row.get("packet_idx"))
        if idx is not None and _int(row.get("functional"), 0) == 1:
            out.add(idx)
    return sorted(out)


# -- the primary mapping (formula, no correlation involved) -------------
def formula_row(packet_idx: int) -> int:
    """The measured primary mapping: ``row = 2 * packet_idx`` (isig=packet_idx,
    pol 0; see the module docstring)."""
    return 2 * int(packet_idx)


def row_packet_idx(row: int) -> int | None:
    """Inverse of :func:`formula_row`; ``None`` for an odd row (pol 1, never
    populated in the deployed configuration -- not a formula row at all)."""
    row = int(row)
    return row // 2 if row % 2 == 0 else None


def formula_mapping(layout: Iterable[dict[str, str]]) -> dict[int, dict[str, Any]]:
    """The unconditional primary mapping: every wired input gets a row, with
    ``status: "formula"`` until a validation pass overlays a checked result."""
    return {
        formula_row(idx): {
            "packet_idx": int(idx),
            "corr": None,
            "runner_up": None,
            "status": "formula",
        }
        for idx in wired_inputs(layout)
    }


# -- visibilities -------------------------------------------------------
def newest_complete_observation(vis_dir: str | os.PathLike[str]) -> tuple[str, int]:
    """(obs base string, index of the newest COMPLETE file) for the live obs.

    "Complete" means the file has the full size of the observation's largest
    file; the live one is still growing and is never read (the plan's guard
    band rule, kept simple here because we only ever want a finished file).
    """
    vis_dir = Path(vis_dir)
    files: dict[str, dict[int, int]] = {}
    for path in vis_dir.glob("*.dat.*"):
        base, _, tail = path.name.rpartition(".dat.")
        idx = _int(tail)
        if idx is None:
            continue
        try:
            files.setdefault(base, {})[idx] = path.stat().st_size
        except OSError:
            continue
    if not files:
        raise RuntimeError(f"no visibility files in {vis_dir}")
    obs = max(files)  # base strings sort chronologically
    sizes = files[obs]
    full = max(sizes.values())
    complete = [idx for idx, size in sizes.items() if size == full]
    if not complete:
        raise RuntimeError(f"observation {obs} has no complete file yet")
    return obs, max(complete)


def read_input_autos(
    vis_dir: str | os.PathLike[str],
    obs: str,
    file_index: int,
    inputs: Sequence[int],
) -> tuple[np.ndarray, np.ndarray]:
    """Time-averaged autocorrelation amplitude per input, (n_inputs, nchan).

    Reuses ``casm_io`` (never a hand-rolled reader) with the canonical
    ``layout_64ant`` format and ``freq_order='descending'`` made explicit.
    """
    from casm_io.correlator.baselines import triu_flat_index
    from casm_io.correlator.formats import load_format
    from casm_io.correlator.reader import VisibilityReader

    ordered = sorted(int(i) for i in inputs)
    reader = VisibilityReader(str(vis_dir), obs, fmt=load_format("layout_64ant"))
    result = reader.read(
        nfiles=1,
        skip_nfiles=int(file_index),
        inputs=list(ordered),
        freq_order="descending",
        verbose=False,
    )
    vis = result["vis"]
    n = len(ordered)
    autos = np.stack(
        [np.abs(vis[:, :, triu_flat_index(n, i, i)]).mean(axis=0) for i in range(n)]
    )
    return autos.astype(np.float64), np.asarray(result["freq_mhz"], dtype=np.float64)


# -- the shape metric ---------------------------------------------------
def band_mask(freq_mhz: np.ndarray) -> np.ndarray:
    """Channels used for the match: the 400-480 MHz part of the band."""
    return (np.asarray(freq_mhz) >= BAND_LO_MHZ) & (np.asarray(freq_mhz) <= BAND_HI_MHZ)


def shape_matrix(power: np.ndarray, mask: np.ndarray, common_n: int = COMMON_MODE_N) -> np.ndarray:
    """Median-normalised log10 shapes with the common bandpass removed."""
    p = np.asarray(power, dtype=np.float64)[:, mask]
    logp = np.log10(np.maximum(p, 1e-12))
    x = logp - np.median(logp, axis=1, keepdims=True)
    if common_n > 0 and x.shape[0] > 1:
        strongest = np.argsort(logp.mean(axis=1))[-min(common_n, x.shape[0]):]
        x = x - np.median(x[strongest], axis=0, keepdims=True)
    return x


def correlation_matrix(
    rows_power: np.ndarray,
    autos: np.ndarray,
    mask: np.ndarray,
    common_n: int = COMMON_MODE_N,
) -> np.ndarray:
    """(n_rows, n_inputs) Pearson correlation of the two shape sets."""
    R = shape_matrix(rows_power, mask, common_n)
    A = shape_matrix(autos, mask, common_n)
    R = R - R.mean(axis=1, keepdims=True)
    A = A - A.mean(axis=1, keepdims=True)
    R /= np.linalg.norm(R, axis=1, keepdims=True) + 1e-30
    A /= np.linalg.norm(A, axis=1, keepdims=True) + 1e-30
    return R @ A.T


def validate_rows(
    rows: Sequence[int],
    rows_power: np.ndarray,
    inputs: Sequence[int],
    autos: np.ndarray,
    freq_mhz: np.ndarray,
    *,
    common_n: int = COMMON_MODE_N,
) -> dict[int, dict[str, Any]]:
    """Validate the formula assignment for every populated row that is some
    wired input's formula row (``row = 2 * packet_idx``).

    This never assigns or rejects a mapping -- the formula already did that
    unconditionally. It only checks whether the row's measured bandpass shape
    best correlates with ITS OWN expected input's autocorrelation
    (``"formula+verified"``) or with some other input's (``"mismatch"``, a red
    flag for the operator). A row that is not any wired input's formula row
    (e.g. an unpopulated pol-1 row, or an unwired ADC) is skipped -- it is not
    part of the mapping at all.
    """
    mask = band_mask(freq_mhz)
    corr = correlation_matrix(rows_power, autos, mask, common_n)
    input_index = {int(p): j for j, p in enumerate(inputs)}
    out: dict[int, dict[str, Any]] = {}
    for k, row in enumerate(rows):
        expected = row_packet_idx(row)
        if expected is None or expected not in input_index:
            continue
        j = input_index[expected]
        scores = corr[k]
        own_score = float(scores[j])
        order = np.argsort(scores)
        best = int(order[-1])
        best_score = float(scores[best])
        verified = best == j
        if verified:
            runner = float(scores[order[-2]]) if scores.size > 1 else float("-inf")
            runner_up = round(runner, 4) if np.isfinite(runner) else None
        else:
            # Report the score of whichever OTHER input beat the expected
            # pairing, so the mismatch is legible in the API/table.
            runner_up = round(best_score, 4)
        out[int(row)] = {
            "packet_idx": expected,
            "corr": round(own_score, 4),
            "runner_up": runner_up,
            "status": "formula+verified" if verified else "mismatch",
        }
    return out


def validate_row_map(
    rows: Sequence[int],
    rows_power: np.ndarray,
    vis_dir: str | os.PathLike[str],
    *,
    layout_path: str | os.PathLike[str] | None = None,
) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    """Validate the formula mapping against the newest complete visibility
    file. Raises if there is no wired input or no usable vis file -- the
    caller (``KafkaBandpassCollector._maybe_row_map``) treats that as "leave
    the existing 'formula'/'formula+verified'/'mismatch' status alone and try
    again next cadence".

    ``layout_path`` defaults to ``rowmap.LAYOUT_CSV`` resolved AT CALL TIME
    (not bound into the signature), so a test's ``monkeypatch.setattr(rowmap,
    "LAYOUT_CSV", ...)`` is honoured.
    """
    layout_path = layout_path if layout_path is not None else LAYOUT_CSV
    inputs = wired_inputs(read_layout(layout_path))
    if not inputs:
        raise RuntimeError(f"no wired inputs in {layout_path}")
    obs, file_index = newest_complete_observation(vis_dir)
    started = time.time()
    autos, freq_mhz = read_input_autos(vis_dir, obs, file_index, inputs)
    mapping = validate_rows(rows, rows_power, inputs, autos, freq_mhz)
    source = {
        "obs": obs,
        "file_index": file_index,
        "n_inputs": len(inputs),
        "layout": str(Path(layout_path).resolve()),
        "common_mode_n": COMMON_MODE_N,
        "band_mhz": [BAND_LO_MHZ, BAND_HI_MHZ],
        "read_s": round(time.time() - started, 2),
    }
    return mapping, source


# -- persistence --------------------------------------------------------
def save_row_map(
    store: Store,
    mapping: dict[int, dict[str, Any]],
    source: dict[str, Any],
    *,
    ts: float | None = None,
) -> None:
    t = time.time() if ts is None else ts
    payload = json.dumps(source, default=str)
    for row, item in sorted(mapping.items()):
        store.execute(
            "INSERT INTO kafka_row_map (row, packet_idx, corr, runner_up, status, ts, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(row) DO UPDATE SET "
            "packet_idx = excluded.packet_idx, corr = excluded.corr, "
            "runner_up = excluded.runner_up, status = excluded.status, "
            "ts = excluded.ts, source = excluded.source",
            (
                int(row),
                None if item.get("packet_idx") is None else int(item["packet_idx"]),
                item.get("corr"),
                item.get("runner_up"),
                item.get("status", "unmapped"),
                t,
                payload,
            ),
        )


def load_row_map(store: Store) -> dict[int, dict[str, Any]]:
    """row -> {...} as stored; empty when the mapping has never been derived."""
    try:
        rows = store.query(
            "SELECT row, packet_idx, corr, runner_up, status, ts, source FROM kafka_row_map"
        )
    except Exception:  # table absent in an older store opened read-only
        return {}
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        try:
            source = json.loads(r["source"]) if r["source"] else {}
        except (TypeError, ValueError):
            source = {}
        out[int(r["row"])] = {
            "packet_idx": None if r["packet_idx"] is None else int(r["packet_idx"]),
            "corr": r["corr"],
            "runner_up": r["runner_up"],
            "status": r["status"],
            "ts": r["ts"],
            "source": source,
        }
    return out


def current_mapping(
    store: Store, layout_path: str | os.PathLike[str] | None = None
) -> dict[int, dict[str, Any]]:
    """The mapping the rest of the app should use: the unconditional formula
    assignment for every wired input, overlaid with whatever validation
    result (``"formula+verified"``/``"mismatch"``) has been persisted for that
    row. A row never in the store answers plain ``"formula"``.

    ``layout_path`` defaults to ``rowmap.LAYOUT_CSV`` resolved at call time
    (see :func:`validate_row_map`)."""
    layout_path = layout_path if layout_path is not None else LAYOUT_CSV
    try:
        layout = read_layout(layout_path)
    except OSError:
        layout = []
    out = formula_mapping(layout)
    for row, item in load_row_map(store).items():
        if row in out:
            # The formula's packet_idx is authoritative and never overridden
            # by the store: an old/stale validation row (in particular any
            # persisted from before this mapping was primary-by-formula, when
            # a rejected row's packet_idx was null) must not blank out a
            # correct formula assignment. Only the validation-derived fields
            # are overlaid.
            status = item.get("status")
            if status not in ("formula+verified", "mismatch"):
                # Either never validated, or a pre-M1 store still carrying
                # the old "mapped"/"unmapped" vocabulary (kept only until the
                # next daily/obs-restart validation overwrites it): treat
                # both the same as "never validated" rather than leaking the
                # legacy label into the API.
                status = "formula"
            out[row] = {
                **out[row],
                "corr": item.get("corr"),
                "runner_up": item.get("runner_up"),
                "status": status,
                "ts": item.get("ts"),
                "source": item.get("source"),
            }
        elif item.get("status") in ("formula+verified", "mismatch") and item.get("packet_idx") is not None:
            # A validation entry for a row the current layout no longer wires
            # as this row's formula input (e.g. a feed just gated): keep it
            # visible, just unlabelled by the formula. A pre-M1 store's
            # "mapped"/"unmapped" rows carry no formula-consistent packet_idx
            # any more (row = 2 * packet_idx may not even hold), so those are
            # simply dropped rather than leaking the old vocabulary.
            out[row] = item
    return out


def assignment(mapping: dict[int, dict[str, Any]]) -> dict[int, int]:
    """row -> packet_idx for every row with a known input.

    The formula assignment is unconditional (a ``"mismatch"`` is a validation
    flag for the operator, not a rejection), so this includes every status
    except a bare ``packet_idx: null`` (never happens for a formula row, but
    kept for stale/legacy entries).
    """
    return {
        int(row): int(item["packet_idx"])
        for row, item in mapping.items()
        if item.get("packet_idx") is not None
    }
