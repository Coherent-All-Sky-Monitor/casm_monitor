"""Bounded observation products, executed only by the existing figure worker."""
from __future__ import annotations

import json
import os
import time

import numpy as np

from ..collectors.rowmap import read_layout
from ..collectors.vis import DT_S, STREAM_AVG8
from ..observation import cache_dir, file_identity, read_json, recorded_product
from ..store.shards import ensure_contained
from ..util import iso

MAX_WEIGHT_BYTES = 400 * 1024**2
MAX_SOLAR_SAMPLES = 1024
MAX_SOLAR_CELLS = 1024 * 384
SOLAR_ANTENNAS = (9, 19)


def inspect_membership(path):
    """All frequencies, polarizations and beams; <=400 MiB total, small chunks."""
    import h5py
    with h5py.File(path, "r") as f:
        weights = f["weights_int8"]
        if weights.ndim != 5 or weights.shape[0] != 2:
            raise ValueError("Unsupported weights axes; expected (real/imag, channel, pol, beam, slot)")
        if weights.size * weights.dtype.itemsize > MAX_WEIGHT_BYTES:
            raise ValueError("Weights exceed the 400 MiB inspection budget")
        ids = np.asarray(f["array_config/antenna_ids"][:], dtype=int)
        positions = np.asarray(f["array_config/positions_enu"][:], dtype=float)
        global_mask = np.asarray(f["array_config/active_mask"][:], dtype=bool)
        nslots = weights.shape[-1]
        if len(ids) < nslots or positions.shape != (len(ids), 3) or np.any(global_mask[nslots:]):
            raise ValueError("Weights slot metadata disagrees with payload")
        # The canonical writer pads metadata to 72 slots, but serializes 64.
        ids, positions, global_mask = ids[:nslots], positions[:nslots], global_mask[:nslots]
        populated = np.zeros((weights.shape[3], weights.shape[-1]), dtype=bool)
        channel_chunk = weights.chunks[1] if weights.chunks else 64
        for start in range(0, weights.shape[1], channel_chunk):
            block = weights[:, start:start + channel_chunk, :, :, :]
            populated |= np.any(block != 0, axis=(0, 1, 2))
        if np.any(populated[:, ids <= 0]):
            raise ValueError("Populated slots have no valid antenna identity")
        names = json.loads(f["pointings"].attrs.get("names", "[]"))
        beams = [{"beam": b, "name": names[b] if b < len(names) else f"Beam {b}",
                  "antennas": sorted(int(a) for a in ids[mask]),
                  "slots": np.flatnonzero(mask).tolist()} for b, mask in enumerate(populated)]
        union = populated.any(axis=0)
        return {"inspection_state": "complete", "membership_kind": "nonzero int8 slots across every stored channel and polarization, per beam",
                "beams": beams, "antennas": sorted(int(a) for a in ids[union]),
                "global_metadata_antennas": sorted(int(a) for a in ids[global_mask] if a > 0),
                "positions": [{"antenna": int(ids[i]), "slot": int(i), "east_m": float(positions[i, 0]),
                               "north_m": float(positions[i, 1])} for i in np.flatnonzero(union)],
                "bytes_inspected": int(weights.size * weights.dtype.itemsize),
                "inspected_at": iso(time.time())}


def _write_json(path, value):
    tmp = path.with_name(f".tmp-{os.getpid()}-{path.name}")
    tmp.write_text(json.dumps(value, allow_nan=False))
    os.replace(tmp, path)


def refresh_membership(store, settings):
    """Inspect recorded weights offline; only write the small membership cache."""
    root = cache_dir(settings)
    root.mkdir(parents=True, exist_ok=True)
    product = recorded_product(settings, store.latest_scalars())
    previous = read_json(root / "membership.json")
    try:
        if not product.get("path"):
            raise ValueError(product.get("error", "No product"))
        identity = file_identity(product["path"])
        if previous.get("identity") == identity and previous.get("product_id") == product["product_id"]:
            membership = previous
        else:
            membership = {**product, **inspect_membership(product["path"]), "identity": identity}
            if file_identity(product["path"]) != identity:
                raise ValueError("Weights changed during inspection")
    except Exception as exc:
        membership = {**product, "inspection_state": "unavailable", "error": str(exc), "beams": [], "antennas": []}
    _write_json(root / "membership.json", membership)
    return membership


def render_observation(store, settings, *, now=None):
    now = time.time() if now is None else now
    membership = refresh_membership(store, settings)
    root = cache_dir(settings)
    try:
        solar = render_solar(store, settings, root, now=now)
        _write_json(root / "solar.json", solar)
    except Exception as exc:
        # Retain last good image and acquisition times, explicitly mark failure.
        solar = read_json(root / "solar.json")
        solar.update(status="unavailable", error=str(exc), attempted_at=iso(now))
        _write_json(root / "solar.json", solar)
    return {"membership": membership["inspection_state"], "solar": solar.get("status")}


def render_solar(store, settings, root, *, now):
    from casm_io.correlator import AntennaMapping
    from casm_vis_analysis.solar_waterfall import plot_dynamic_spectrum
    from ..web.vis import VisStore, freq_axis
    rows = {int(r["antenna"]): r for r in read_layout(settings.layout_csv)}
    if any(a not in rows or rows[a].get("functional") != "1" for a in SOLAR_ANTENNAS):
        raise ValueError("Selected solar-context baseline 9 × 19 is not wired in current layout")
    mapping = AntennaMapping.load(settings.layout_csv)
    pair = tuple(mapping.packet_index(a) for a in SOLAR_ANTENNAS)
    view = VisStore(settings, store)
    shards = view.shards.list(STREAM_AVG8, t0=now - 86400, t1=now)
    if not shards or len(shards) > MAX_SOLAR_SAMPLES:
        raise ValueError("No bounded 24-hour averaged visibility cache available")
    expected_freq = None
    samples = cells = 0
    for shard in shards:
        ensure_contained(shard["path"], settings.store_root)
        shape = shard["shape"]
        nt, nf = (1 if len(shape) == 2 else shape[0]), shape[-1]
        samples += nt
        cells += nt * nf
        if samples > MAX_SOLAR_SAMPLES or cells > MAX_SOLAR_CELLS:
            raise ValueError("Solar cache selection exceeds 1024 samples / 393216 cells")
        meta = shard.get("meta") or {}
        if "freq_top_mhz" not in meta or "chan_bw_mhz" not in meta:
            raise ValueError("Visibility cache lacks explicit frequency coordinates")
        axis = freq_axis(meta, nf)
        if expected_freq is not None and not np.array_equal(expected_freq, axis):
            raise ValueError("Frequency setup changed inside solar-context window")
        expected_freq = axis
    z, times, freq, _ = view.series(STREAM_AVG8, now - 86400, now, [pair], allow_full_fallback=False)
    if len(times) < 2 or not np.isfinite(z).any():
        raise ValueError("Not enough valid baseline samples")
    layout_identity = file_identity(settings.layout_csv)
    old = read_json(root / "solar.json")
    signature = {"t1": float(times[-1]), "samples": len(times), "layout": layout_identity,
                 "pair": list(pair), "shards": [s["id"] for s in shards], "renderer": 2}
    if old.get("signature") == signature and (root / "solar.png").is_file():
        return {**old, "status": "ready", "error": None}
    labels = [mapping.format_antenna(a) + f" ({rows[a].get('row', '')}{rows[a].get('col', '')})" for a in SOLAR_ANTENNAS]
    title = "Solar context · " + " × ".join(labels)
    tmp = root / f".tmp-{os.getpid()}-solar.png"
    plot_dynamic_spectrum(np.abs(z[:, 0, :]), times, freq, tmp, title=title,
                          quantity="Correlated amplitude", integration_s=DT_S)
    os.replace(tmp, root / "solar.png")
    return {"status": "ready", "signature": signature, "observed_start": iso(float(times[0])),
            "observed_end": iso(float(times[-1])), "rendered_at": iso(time.time()),
            "n_integrations": len(times), "integration_s": DT_S, "stream": STREAM_AVG8,
            "antenna_ids": list(SOLAR_ANTENNAS), "baseline_labels": labels,
            "frequency_mhz": [float(freq.min()), float(freq.max())], "n_channels": len(freq),
            "caption": "Raw cross-correlation amplitude on one baseline, normalized by each channel's mean over this window. 8-channel complex averages; no calibration or background subtraction. Stored timestamps with nominal integration widths; timestamp start/centre/end convention is not independently verified. Solar context only: other sources and interference contribute. Not flux or beamformed power; variability alone is not a fault."}
