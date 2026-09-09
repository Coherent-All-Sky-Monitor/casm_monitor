"""Reference transforms for the Visibilities tab: quantity, units, reference.

Pure functions on numpy arrays -- no store, no files, no FastAPI. The web layer
(:mod:`casm_monitor.web.vis`) selects baselines out of a shard and hands the
complex visibilities here; the collector never uses this module.

Conventions (plan.md "Visibilities" + the wiki's vis-beamforming page):

* a baseline is a pair of RANKS in the sorted input subset stored in the shard;
  ``input = packet_idx = antenna_id - 1``,
* the stored element for ranks ``i <= j`` is ``V_ij`` and its geometric phase
  goes with the baseline vector ``r_j - r_i`` (the same pairing
  ``casm_vis_analysis.fringe_stop`` uses for ref->target), so fringe-stopping
  uses ``sign = -1`` with ``tau = (r_j - r_i) . s_hat / c``,
* channel averaging is done in LINEAR POWER for amplitude and in the COMPLEX
  plane for phase and coherence -- averaging dB or degrees would bias both,
* ``coh = |V_ij| / sqrt(A_i A_j)`` is defined for cross baselines only.

Units, exactly as the toggle bar offers them:

===========  ==============================================================
quantity     units
===========  ==============================================================
amp          ``linear`` | ``db`` (= 10 log10 |V|^2 = 20 log10 |V|) | ``log10``
phase        ``deg`` | ``rad``
real, imag   ``linear`` | ``db`` (= sign(x) * 10 log10 |x|) | ``log10``
coh          ``linear`` (dimensionless; no other unit is accepted)
===========  ==============================================================
"""

from __future__ import annotations

import warnings
from typing import Any, Sequence

import numpy as np

QUANTITIES: tuple[str, ...] = ("amp", "phase", "real", "imag", "coh")
REFS: tuple[str, ...] = ("raw", "sun", "cal")

UNITS_FOR: dict[str, tuple[str, ...]] = {
    "amp": ("linear", "db", "log10"),
    "real": ("linear", "db", "log10"),
    "imag": ("linear", "db", "log10"),
    "phase": ("deg", "rad"),
    "coh": ("linear",),
}
DEFAULT_UNITS: dict[str, str] = {
    "amp": "db",
    "real": "linear",
    "imag": "linear",
    "phase": "deg",
    "coh": "linear",
}

# Floor for the log conversions: a dead (identically zero) baseline must plot as
# a number, the same way kafka_bp.to_db floors a dead input.
LOG_FLOOR = 1e-12
# Fringe-stop sign convention (casm_vis_analysis.fringe_stop removes the
# geometric phase with sign=-1). Never a caller-facing parameter.
FRINGE_STOP_SIGN = -1


class ParamError(ValueError):
    """A bad quantity/units/ref combination (the router turns this into 400)."""


# -- parameter validation ----------------------------------------------
def check_params(
    quantity: str | None,
    units: str | None = None,
    ref: str | None = None,
    *,
    pairs: str | None = None,
) -> tuple[str, str, str]:
    """Validate and default ``(quantity, units, ref)``; raise on nonsense.

    ``units=None`` picks :data:`DEFAULT_UNITS`. ``pairs`` is only inspected to
    refuse coherence on autocorrelations, which is not a defined quantity.
    """
    q = (quantity or "amp").strip().lower()
    if q not in QUANTITIES:
        raise ParamError(f"quantity must be one of {'|'.join(QUANTITIES)}")
    u = (units or DEFAULT_UNITS[q]).strip().lower()
    if u not in UNITS_FOR[q]:
        raise ParamError(f"units for {q} must be one of {'|'.join(UNITS_FOR[q])}")
    r = (ref or "raw").strip().lower()
    if r not in REFS:
        raise ParamError(f"ref must be one of {'|'.join(REFS)}")
    if q == "coh" and pairs == "auto":
        raise ParamError("coherence is defined for cross baselines only (pairs=cross|all)")
    return q, u, r


def units_label(quantity: str, units: str) -> str:
    """Axis label for the chosen combination, spelled out where it matters."""
    if quantity == "phase":
        return "deg" if units == "deg" else "rad"
    if quantity == "coh":
        return "|V|/sqrt(Ai Aj)"
    if units == "db":
        return "dB" if quantity == "amp" else "dB (sign x 10log10|x|)"
    if units == "log10":
        return "log10" if quantity == "amp" else "sign x log10|x|"
    return "counts"


# -- channel averaging --------------------------------------------------
def _nanmean(arr: np.ndarray, *, axis: int, dtype: Any, keepdims: bool = False) -> np.ndarray:
    """``np.nanmean`` with the "all-NaN slice" RuntimeWarning silenced.

    A block that is entirely flagged (every channel of it NaN, e.g. a whole
    ``cal`` gap) legitimately averages to NaN; that is not a bug to warn
    about, it is exactly what a flagged block should report.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        return np.nanmean(arr, axis=axis, dtype=dtype, keepdims=keepdims)


def block_mean(x: np.ndarray, nchan: int | None) -> np.ndarray:
    """Block-average the LAST axis down to at most ``nchan`` points.

    Works for real and complex input (the complex mean is what phase and
    coherence need). A ragged tail becomes one shorter final block rather than
    being dropped, so the band edge is never silently lost.

    The reduction is NaN-aware (``nanmean``, not ``mean``): a reference like
    ``cal`` flags individual channels/baselines as NaN (a missing gain, a
    flagged cal channel), and a plain mean would let that ONE bad channel null
    the entire averaged block instead of just being excluded from it. A block
    that is entirely flagged still (correctly) averages to NaN.

    The accumulation is always in 64-bit: the shards are complex64, and summing
    3072 of them in single precision costs ~1e-5 of relative accuracy, which is
    enough to make a coherence of exactly 1 come back as 0.99997.
    """
    arr = np.asarray(x)
    n = arr.shape[-1]
    acc = np.complex128 if np.iscomplexobj(arr) else np.float64
    if nchan is None or nchan <= 0 or nchan >= n:
        return arr
    block = int(np.ceil(n / nchan))
    keep = (n // block) * block
    head = _nanmean(
        arr[..., :keep].reshape(*arr.shape[:-1], keep // block, block), axis=-1, dtype=acc
    )
    if keep == n:
        return head
    tail = _nanmean(arr[..., keep:], axis=-1, dtype=acc, keepdims=True)
    return np.concatenate([head, tail], axis=-1)


def average_for_quantity(
    v: np.ndarray, nchan: int | None, quantity: str
) -> tuple[np.ndarray, np.ndarray | None]:
    """Channel-average ``v`` the way ``quantity`` requires.

    Returns ``(v_avg, power_avg)``: the complex block mean always, plus the
    block mean of ``|V|^2`` when the quantity is an amplitude (so ``amp`` is an
    rms amplitude over the block, not the amplitude of a vector mean that a
    fringe would cancel).
    """
    v = np.asarray(v)
    v_avg = block_mean(v, nchan)
    power_avg = None
    if quantity == "amp":
        power_avg = block_mean(np.abs(v) ** 2, nchan)
    return v_avg, power_avg


# -- quantity + units ---------------------------------------------------
def quantity_values(
    v: np.ndarray,
    quantity: str,
    *,
    power: np.ndarray | None = None,
    auto_i: np.ndarray | None = None,
    auto_j: np.ndarray | None = None,
) -> np.ndarray:
    """The raw (linear/radian) value of ``quantity`` from complex visibilities.

    ``power`` is the block-averaged ``|V|^2`` when one was formed;
    ``auto_i``/``auto_j`` are the two autocorrelation values on the same axis,
    i.e. ``A_i = |V_ii|`` (the stored autocorrelation element IS the power, so it
    is NOT squared again), required for ``coh``.
    """
    v = np.asarray(v)
    if quantity == "amp":
        p = np.abs(v) ** 2 if power is None else np.asarray(power, dtype=np.float64)
        return np.sqrt(np.maximum(p, 0.0))
    if quantity == "phase":
        return np.angle(v)
    if quantity == "real":
        return v.real.astype(np.float64)
    if quantity == "imag":
        return v.imag.astype(np.float64)
    if quantity == "coh":
        if auto_i is None or auto_j is None:
            raise ParamError("coherence needs both autocorrelations")
        denom = np.sqrt(
            np.maximum(np.asarray(auto_i, dtype=np.float64), 0.0)
            * np.maximum(np.asarray(auto_j, dtype=np.float64), 0.0)
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            out = np.abs(v) / denom
        return np.where(denom > 0, out, np.nan)
    raise ParamError(f"unknown quantity {quantity!r}")


def apply_units(values: np.ndarray, quantity: str, units: str) -> np.ndarray:
    """Convert the linear/radian values of ``quantity`` into ``units``."""
    x = np.asarray(values, dtype=np.float64)
    if quantity == "phase":
        return np.degrees(x) if units == "deg" else x
    if quantity == "coh" or units == "linear":
        return x
    if quantity == "amp":
        # dB of the POWER |V|^2 (= 20 log10 |V|), the same convention the
        # bandpass views use; log10 is of the amplitude itself.
        floored = np.maximum(x, LOG_FLOOR)
        return 20.0 * np.log10(floored) if units == "db" else np.log10(floored)
    # real/imag: signed, so the magnitude is converted and the sign kept.
    sign = np.sign(x)
    mag = np.maximum(np.abs(x), LOG_FLOOR)
    return sign * (10.0 * np.log10(mag) if units == "db" else np.log10(mag))


def spectra_values(
    v: np.ndarray,
    quantity: str,
    units: str,
    *,
    nchan: int | None = None,
    auto_i: np.ndarray | None = None,
    auto_j: np.ndarray | None = None,
) -> np.ndarray:
    """Channel-average, form the quantity, convert the units -- in that order.

    ``auto_i``/``auto_j`` are the full-resolution autocorrelation values
    ``A = |V_ii|`` on the same frequency axis as ``v``; they are averaged
    linearly (they are already powers) over the same blocks.
    """
    v_avg, power_avg = average_for_quantity(v, nchan, quantity)
    ai = None if auto_i is None else block_mean(np.asarray(auto_i, dtype=np.float64), nchan)
    aj = None if auto_j is None else block_mean(np.asarray(auto_j, dtype=np.float64), nchan)
    values = quantity_values(v_avg, quantity, power=power_avg, auto_i=ai, auto_j=aj)
    return apply_units(values, quantity, units)


# -- reference: fringe-stop toward the Sun ------------------------------
def baseline_vectors(positions_enu: np.ndarray, pairs: Sequence[tuple[int, int]]) -> np.ndarray:
    """``r_j - r_i`` in ENU metres for every ``(rank_i, rank_j)`` pair.

    Built with :func:`casm_vis_analysis.fringe_stop.compute_baselines_enu` (one
    call per distinct ``i``) so the sign convention comes from that module and
    is not re-derived here.
    """
    from casm_vis_analysis.fringe_stop import compute_baselines_enu

    positions = np.asarray(positions_enu, dtype=np.float64)
    out = np.zeros((len(pairs), 3), dtype=np.float64)
    by_i: dict[int, list[int]] = {}
    for k, (i, _j) in enumerate(pairs):
        by_i.setdefault(int(i), []).append(k)
    for i, ks in by_i.items():
        targets = [int(pairs[k][1]) for k in ks]
        out[ks] = compute_baselines_enu(positions, i, targets)
    return out


def sun_delays(
    positions_enu: np.ndarray,
    pairs: Sequence[tuple[int, int]],
    times_unix: Sequence[float] | np.ndarray,
) -> np.ndarray:
    """Geometric delay ``tau = (r_j - r_i) . s_hat / c`` toward the Sun, (T, n_bl)."""
    from casm_vis_analysis.fringe_stop import geometric_delay
    from casm_vis_analysis.sources import source_enu

    times = np.atleast_1d(np.asarray(times_unix, dtype=np.float64))
    s_enu = source_enu("sun", times)
    tau = geometric_delay(s_enu, baseline_vectors(positions_enu, pairs))
    return np.atleast_2d(np.asarray(tau, dtype=np.float64))


def fringe_stop_tfb(
    v_tfb: np.ndarray, freq_mhz: np.ndarray, tau_s: np.ndarray, sign: int = FRINGE_STOP_SIGN
) -> np.ndarray:
    """Fringe-stop a ``(T, F, n_bl)`` cube with ``casm_vis_analysis``."""
    from casm_vis_analysis.fringe_stop import fringe_stop_array

    out = fringe_stop_array(
        np.asarray(v_tfb), np.asarray(freq_mhz, dtype=np.float64), np.asarray(tau_s), sign=sign
    )
    return np.asarray(out["vis_stopped"])


def fringe_stop_sun(
    v: np.ndarray,
    freq_mhz: np.ndarray,
    positions_enu: np.ndarray,
    pairs: Sequence[tuple[int, int]],
    times_unix: Sequence[float] | np.ndarray,
) -> np.ndarray:
    """Fringe-stop toward the Sun. ``v`` is ``(n_bl, F)`` or ``(T, n_bl, F)``.

    The output keeps the input's shape; ``times_unix`` must have one entry per
    time sample (a single float for the 2-D form).
    """
    v = np.asarray(v)
    single = v.ndim == 2
    cube = v[None] if single else v
    times = np.atleast_1d(np.asarray(times_unix, dtype=np.float64))
    if times.size != cube.shape[0]:
        raise ParamError(
            f"need one timestamp per sample: {times.size} times for {cube.shape[0]} samples"
        )
    tau = sun_delays(positions_enu, pairs, times)
    stopped = fringe_stop_tfb(np.swapaxes(cube, 1, 2), freq_mhz, tau)
    out = np.swapaxes(stopped, 1, 2)
    return out[0] if single else out


# -- reference: divide by the deployed cal ------------------------------
def cal_gains_on_axis(
    cal: Any, inputs: Sequence[int], freq_mhz: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Per-input complex gains resampled onto ``freq_mhz``.

    ``cal`` is whatever ``bf_weights_generator.load_calibration_weights``
    returned: ``weights = conj(gain)`` over ``(n_ant, n_chan)`` with ASCENDING
    ``frequencies_hz`` and 1-indexed ``ant_ids``. The wiki trap applies here:
    ``ant_id - 1 = packet_idx = correlator input``.

    Returns ``(gains, have)`` with ``gains`` (n_inputs, F) complex128 and
    ``have`` (n_inputs,) bool -- False for an input the cal file does not carry
    (its baselines come back NaN rather than silently uncalibrated).
    """
    weights = np.asarray(cal.weights)
    ant_ids = np.asarray(cal.ant_ids, dtype=int)
    cal_freq_mhz = np.asarray(cal.frequencies_hz, dtype=np.float64) / 1e6
    flags = np.asarray(cal.flags, dtype=bool)
    target = np.asarray(freq_mhz, dtype=np.float64)
    # Nearest cal channel per target channel (the two grids are the same
    # correlator axis in practice; nearest keeps a chan-averaged axis usable).
    idx = np.abs(target[:, None] - cal_freq_mhz[None, :]).argmin(axis=1)
    row_of_input = {int(a) - 1: k for k, a in enumerate(ant_ids)}
    gains = np.zeros((len(inputs), target.size), dtype=np.complex128)
    have = np.zeros(len(inputs), dtype=bool)
    for n, packet_idx in enumerate(inputs):
        row = row_of_input.get(int(packet_idx))
        if row is None:
            continue
        g = np.conj(np.asarray(weights[row], dtype=np.complex128))[idx]
        g[~flags[idx]] = 0.0
        gains[n] = g
        have[n] = True
    return gains, have


def cal_auto_power_scale(gains: np.ndarray, have: np.ndarray) -> np.ndarray:
    """``1 / |g_i|^2`` per input, aligned to ``gains``' rows.

    An autocorrelation IS a power (``A_i = |V_ii|``), and a calibrated cross
    is the raw one divided by ``g_i conj(g_j)``; the matching calibrated auto
    is therefore ``A_i / |g_i|^2``, never the raw ``A_i`` (a coherence built
    from a calibrated numerator and a raw denominator is not a coherence of
    anything -- 2026-09-09 review). NaN where the input has no cal gain or a
    flagged (zero) channel, the same convention :func:`apply_cal` uses for a
    missing baseline end.
    """
    power = np.abs(np.asarray(gains)) ** 2
    with np.errstate(divide="ignore", invalid="ignore"):
        scale = 1.0 / power
    ok = np.asarray(have, dtype=bool)[:, None] & (power > 0)
    return np.where(ok, scale, np.nan)


def apply_cal(
    v: np.ndarray,
    gains: np.ndarray,
    have: np.ndarray,
    pairs: Sequence[tuple[int, int]],
) -> np.ndarray:
    """Divide by ``g_i conj(g_j)`` per baseline. ``v`` is (n_bl, F) or (T, n_bl, F).

    A baseline whose either end is missing from the cal, or whose channel is
    flagged (gain 0), comes back NaN.
    """
    v = np.asarray(v)
    gains = np.asarray(gains)
    i_idx = np.array([int(i) for i, _ in pairs], dtype=int)
    j_idx = np.array([int(j) for _, j in pairs], dtype=int)
    factor = gains[i_idx] * np.conj(gains[j_idx])
    ok = np.asarray(have, dtype=bool)[i_idx] & np.asarray(have, dtype=bool)[j_idx]
    factor = np.where(ok[:, None], factor, np.nan + 0j)
    good = np.abs(factor) > 0
    if v.ndim != 2:
        factor = factor[None]
        good = good[None]
    with np.errstate(divide="ignore", invalid="ignore"):
        out = v / factor
    return np.where(good, out, np.nan + 0j)


# -- decimation ---------------------------------------------------------
def decimation_targets(nt: int, nf: int, max_cells: int) -> tuple[int, int]:
    """Output ``(n_times, n_channels)`` for a ``(T, F)`` viewport budget.

    The aspect ratio is kept where it can be, and ``n_times * n_channels <=
    max_cells`` holds strictly -- a time-heavy matrix collapses frequency to a
    single channel rather than returning more cells than asked for.
    """
    if nt == 0 or nf == 0 or nt * nf <= max_cells:
        return nt, nf
    scale = np.sqrt((nt * nf) / float(max_cells))
    target_t = min(max(1, int(np.floor(nt / max(1.0, scale)))), int(max_cells))
    target_t = min(target_t, nt)
    target_f = min(nf, max(1, int(max_cells // max(1, target_t))))
    return target_t, target_f


def decimate_axes(
    z: np.ndarray, target_t: int, target_f: int
) -> np.ndarray:
    """Block-average a ``(T, F)`` array onto ``(target_t, target_f)``.

    Complex input is averaged in the complex plane, real input linearly; a
    caller that wants amplitudes averages the POWER and takes the root
    afterwards.
    """
    z = np.asarray(z)
    if target_t < z.shape[0]:
        z = np.swapaxes(block_mean(np.swapaxes(z, 0, 1), target_t), 0, 1)
    if target_f < z.shape[1]:
        z = block_mean(z, target_f)
    return z


def decimate_cube(
    z: np.ndarray,
    times: np.ndarray,
    freq: np.ndarray,
    max_cells: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Reduce a ``(T, F)`` matrix to at most ``max_cells`` cells.

    Both axes are block-averaged (complex or power, never dB/degrees), the
    aspect ratio is kept where possible, and ``len(t) * len(freq) <= max_cells``
    holds strictly on the way out.
    """
    z = np.asarray(z)
    times = np.asarray(times, dtype=np.float64)
    freq = np.asarray(freq, dtype=np.float64)
    target_t, target_f = decimation_targets(z.shape[0], z.shape[1], max_cells)
    if (target_t, target_f) == z.shape:
        return z, times, freq
    return (
        decimate_axes(z, target_t, target_f),
        block_mean(times, target_t),
        block_mean(freq, target_f),
    )


__all__ = [
    "DEFAULT_UNITS",
    "FRINGE_STOP_SIGN",
    "LOG_FLOOR",
    "ParamError",
    "QUANTITIES",
    "REFS",
    "UNITS_FOR",
    "apply_cal",
    "apply_units",
    "average_for_quantity",
    "baseline_vectors",
    "block_mean",
    "cal_auto_power_scale",
    "cal_gains_on_axis",
    "check_params",
    "decimate_axes",
    "decimate_cube",
    "decimation_targets",
    "fringe_stop_sun",
    "fringe_stop_tfb",
    "quantity_values",
    "spectra_values",
    "sun_delays",
    "units_label",
]
