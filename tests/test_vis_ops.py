"""Reference transforms: every quantity/units combo, the fringe-stop sign, cal,
and the decimation bounds. All synthetic -- nothing here reads /mnt."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from casm_monitor import vis_ops

FREQ = np.linspace(484.375, 390.656, 64)


# -- parameters ---------------------------------------------------------
def test_every_quantity_has_a_default_and_accepts_its_units() -> None:
    for quantity in vis_ops.QUANTITIES:
        q, u, r = vis_ops.check_params(quantity, None, None)
        assert (q, u, r) == (quantity, vis_ops.DEFAULT_UNITS[quantity], "raw")
        for units in vis_ops.UNITS_FOR[quantity]:
            for ref in vis_ops.REFS:
                assert vis_ops.check_params(quantity, units, ref) == (quantity, units, ref)


@pytest.mark.parametrize(
    "quantity,units,ref",
    [
        ("nope", None, "raw"),
        ("amp", "deg", "raw"),
        ("phase", "db", "raw"),
        ("coh", "db", "raw"),
        ("real", "rad", "raw"),
        ("amp", "db", "moon"),
    ],
)
def test_bad_combinations_raise(quantity, units, ref) -> None:
    with pytest.raises(vis_ops.ParamError):
        vis_ops.check_params(quantity, units, ref)


def test_coherence_refuses_autocorrelations() -> None:
    with pytest.raises(vis_ops.ParamError):
        vis_ops.check_params("coh", None, "raw", pairs="auto")
    # ... but is fine on crosses and on the mixed set
    assert vis_ops.check_params("coh", None, "raw", pairs="cross")[0] == "coh"
    assert vis_ops.check_params("coh", None, "raw", pairs="all")[0] == "coh"


# -- units --------------------------------------------------------------
def test_amp_units() -> None:
    v = np.array([[2.0 + 0j, 0.0 + 0j]])
    linear = vis_ops.spectra_values(v, "amp", "linear")
    assert linear[0, 0] == pytest.approx(2.0)
    db = vis_ops.spectra_values(v, "amp", "db")
    assert db[0, 0] == pytest.approx(20 * np.log10(2.0))
    # A dead baseline plots as a floored number, never -inf or NaN.
    assert np.isfinite(db).all()
    assert db[0, 1] == pytest.approx(20 * np.log10(vis_ops.LOG_FLOOR))
    log10 = vis_ops.spectra_values(v, "amp", "log10")
    assert log10[0, 0] == pytest.approx(np.log10(2.0))


def test_phase_units() -> None:
    v = np.array([[1j, -1.0 + 0j]])
    rad = vis_ops.spectra_values(v, "phase", "rad")
    deg = vis_ops.spectra_values(v, "phase", "deg")
    assert rad[0, 0] == pytest.approx(np.pi / 2)
    assert deg[0, 0] == pytest.approx(90.0)
    assert deg[0, 1] == pytest.approx(180.0)


def test_real_imag_units_keep_the_sign_in_db() -> None:
    v = np.array([[-100.0 + 10.0j]])
    assert vis_ops.spectra_values(v, "real", "linear")[0, 0] == pytest.approx(-100.0)
    assert vis_ops.spectra_values(v, "real", "db")[0, 0] == pytest.approx(-20.0)
    assert vis_ops.spectra_values(v, "real", "log10")[0, 0] == pytest.approx(-2.0)
    assert vis_ops.spectra_values(v, "imag", "db")[0, 0] == pytest.approx(10.0)


def test_coherence_is_one_for_a_perfectly_correlated_pair() -> None:
    a_i = np.full(8, 4.0)
    a_j = np.full(8, 9.0)
    v = np.full((1, 8), np.sqrt(4.0 * 9.0) + 0j)
    coh = vis_ops.spectra_values(v, "coh", "linear", auto_i=a_i[None], auto_j=a_j[None])
    assert coh[0] == pytest.approx(np.ones(8))


def test_coherence_needs_both_autos() -> None:
    with pytest.raises(vis_ops.ParamError):
        vis_ops.quantity_values(np.ones((1, 4), dtype=complex), "coh")


def test_dead_input_coherence_is_nan_not_a_division_error() -> None:
    coh = vis_ops.quantity_values(
        np.ones((1, 3), dtype=complex), "coh", auto_i=np.zeros((1, 3)), auto_j=np.ones((1, 3))
    )
    assert np.isnan(coh).all()


# -- channel averaging --------------------------------------------------
def test_block_mean_shapes_and_ragged_tail() -> None:
    x = np.arange(10, dtype=float)
    assert vis_ops.block_mean(x, 10).tolist() == x.tolist()
    assert vis_ops.block_mean(x, 20).tolist() == x.tolist()
    out = vis_ops.block_mean(x, 3)
    assert out.size <= 4  # 3 full blocks of 4 does not fit 10, so 2 + a tail
    assert vis_ops.block_mean(x, 5).tolist() == [0.5, 2.5, 4.5, 6.5, 8.5]
    z = np.arange(12, dtype=complex).reshape(2, 6)
    assert vis_ops.block_mean(z, 3).shape == (2, 3)


def test_amp_averages_in_power_and_phase_in_the_complex_plane() -> None:
    # Two channels in antiphase: the complex mean is zero, the rms amplitude is
    # not. Averaging in dB or in the complex plane would report a dead baseline.
    v = np.array([[1.0 + 0j, -1.0 + 0j]])
    assert vis_ops.spectra_values(v, "amp", "linear", nchan=1)[0, 0] == pytest.approx(1.0)
    # Phase, by contrast, IS the vector mean's angle.
    v2 = np.array([[np.exp(1j * 0.1), np.exp(1j * 0.3)]])
    assert vis_ops.spectra_values(v2, "phase", "rad", nchan=1)[0, 0] == pytest.approx(0.2, abs=1e-3)


# -- fringe stop --------------------------------------------------------
def _synthetic_point_source(positions, pairs, s_hat, freq_mhz):
    """V_ij for a unit point source at ``s_hat``: exp(+i 2 pi f tau_ij)."""
    from casm_io.constants import C_LIGHT_M_S

    b = vis_ops.baseline_vectors(positions, pairs)
    tau = b @ np.asarray(s_hat, dtype=float) / C_LIGHT_M_S
    phase = 2 * np.pi * tau[:, None] * (np.asarray(freq_mhz) * 1e6)[None, :]
    return np.exp(1j * phase), tau


def test_baseline_vectors_are_rj_minus_ri() -> None:
    positions = np.array([[0.0, 0, 0], [10.0, 0, 0], [0.0, 5.0, 0]])
    pairs = [(0, 1), (1, 2), (0, 0)]
    b = vis_ops.baseline_vectors(positions, pairs)
    assert b[0].tolist() == [10.0, 0.0, 0.0]
    assert b[1].tolist() == [-10.0, 5.0, 0.0]
    assert b[2].tolist() == [0.0, 0.0, 0.0]


def test_fringe_stop_sign_makes_a_point_source_coherent() -> None:
    # Baselines long enough that 93.75 MHz of band holds several fringe cycles.
    positions = np.array([[0.0, 0, 0], [120.0, 30.0, 1.0], [-70.0, 90.0, 0.0], [40.0, -60.0, 0.5]])
    pairs = [(a, b) for a in range(4) for b in range(a + 1, 4)]
    s_hat = np.array([0.3, 0.2, np.sqrt(1 - 0.09 - 0.04)])
    v, tau = _synthetic_point_source(positions, pairs, s_hat, FREQ)
    autos = np.ones((len(pairs), FREQ.size))

    def band_coherence(vv):
        return np.abs(vv.mean(axis=1)) / np.sqrt(autos.mean(axis=1) ** 2)

    # A fringe only decorrelates the band average once the delay-bandwidth
    # product is large, so the "raw is incoherent" half of the check is asserted
    # on the baselines where that holds.
    bandwidth_hz = (FREQ.max() - FREQ.min()) * 1e6
    fringing = np.abs(tau) * bandwidth_hz > 2.0
    assert fringing.any()
    raw = band_coherence(v)
    assert raw[fringing].max() < 0.5

    cube = np.swapaxes(v[None], 1, 2)
    stopped = np.swapaxes(
        vis_ops.fringe_stop_tfb(cube, FREQ, tau[None], sign=vis_ops.FRINGE_STOP_SIGN), 1, 2
    )[0]
    assert band_coherence(stopped) == pytest.approx(np.ones(len(pairs)), abs=1e-6)

    # The opposite sign doubles the fringe instead of removing it.
    wrong = np.swapaxes(vis_ops.fringe_stop_tfb(cube, FREQ, tau[None], sign=+1), 1, 2)[0]
    assert band_coherence(wrong)[fringing].max() < 0.5


def test_fringe_stop_sun_shapes_and_zero_baseline() -> None:
    positions = np.array([[0.0, 0, 0], [30.0, 10.0, 0.0]])
    pairs = [(0, 0), (0, 1), (1, 1)]
    times = [1788899767.0, 1788899904.0]
    tau = vis_ops.sun_delays(positions, pairs, times)
    assert tau.shape == (2, 3)
    # An autocorrelation has no geometric delay, ever.
    assert tau[:, 0] == pytest.approx(np.zeros(2))
    assert tau[:, 2] == pytest.approx(np.zeros(2))
    assert abs(tau[0, 1]) > 0
    v = np.ones((2, 3, FREQ.size), dtype=complex)
    out = vis_ops.fringe_stop_sun(v, FREQ, positions, pairs, times)
    assert out.shape == v.shape
    assert out[:, 0, :] == pytest.approx(np.ones((2, FREQ.size)))
    single = vis_ops.fringe_stop_sun(v[0], FREQ, positions, pairs, times[0])
    assert single.shape == (3, FREQ.size)


def test_fringe_stop_sun_refuses_a_time_axis_mismatch() -> None:
    with pytest.raises(vis_ops.ParamError):
        vis_ops.fringe_stop_sun(
            np.ones((3, 1, FREQ.size), dtype=complex),
            FREQ,
            np.zeros((2, 3)),
            [(0, 1)],
            [1.0, 2.0],
        )


# -- cal ----------------------------------------------------------------
def _fake_cal(n_ant=3, nchan=64, flagged=()):
    rng = np.random.default_rng(7)
    gains = np.exp(1j * rng.uniform(-np.pi, np.pi, size=(n_ant, nchan)))
    flags = np.ones(nchan, dtype=bool)
    for ch in flagged:
        flags[ch] = False
    return SimpleNamespace(
        weights=np.conj(gains),                       # the file stores conj(g)
        gains=gains,
        flags=flags,
        frequencies_hz=np.sort(FREQ)[: nchan] * 1e6,  # ASCENDING, as loaded
        ant_ids=np.arange(1, n_ant + 1),              # 1-indexed antenna numbers
        ref_ant_id=1,
        source="sun",
    ), gains


def test_cal_divides_out_gi_conj_gj() -> None:
    cal, gains = _fake_cal()
    inputs = [0, 1, 2]  # ant_id - 1
    pairs = [(0, 1), (0, 2), (1, 2)]
    g, have = vis_ops.cal_gains_on_axis(cal, inputs, FREQ)
    assert have.all()
    # gains are returned on the request's (descending) axis
    assert g.shape == (3, FREQ.size)
    truth = np.ones((len(pairs), FREQ.size), dtype=complex)
    corrupted = np.stack([g[a] * np.conj(g[b]) * truth[k] for k, (a, b) in enumerate(pairs)])
    recovered = vis_ops.apply_cal(corrupted, g, have, pairs)
    assert recovered == pytest.approx(truth, abs=1e-9)


def test_cal_marks_unknown_inputs_and_flagged_channels() -> None:
    cal, _ = _fake_cal(flagged=(0, 5))
    inputs = [0, 9]  # antenna 10 is not in the cal file
    pairs = [(0, 1)]
    g, have = vis_ops.cal_gains_on_axis(cal, inputs, FREQ)
    assert have.tolist() == [True, False]
    out = vis_ops.apply_cal(np.ones((1, FREQ.size), dtype=complex), g, have, pairs)
    assert np.isnan(out).all()
    # a known pair still goes NaN on the flagged channels only
    cal2, _ = _fake_cal(flagged=(0, 5))
    g2, have2 = vis_ops.cal_gains_on_axis(cal2, [0, 1], FREQ)
    out2 = vis_ops.apply_cal(np.ones((1, FREQ.size), dtype=complex), g2, have2, [(0, 1)])
    nan_channels = np.flatnonzero(np.isnan(out2[0]))
    # the descending request axis maps onto the ascending cal axis
    assert nan_channels.size == 2


# -- decimation ---------------------------------------------------------
@pytest.mark.parametrize(
    "nt,nf,max_cells",
    [(1, 1, 10), (10, 10, 1000), (400, 3072, 400_000), (5000, 384, 1000), (7, 3072, 1000), (3, 5, 1)],
)
def test_decimation_never_exceeds_the_budget(nt, nf, max_cells) -> None:
    target_t, target_f = vis_ops.decimation_targets(nt, nf, max_cells)
    assert 1 <= target_t <= nt
    assert 1 <= target_f <= nf
    assert target_t * target_f <= max(max_cells, 1)
    z = (np.arange(nt * nf, dtype=float) + 1j).reshape(nt, nf)
    out, t, f = vis_ops.decimate_cube(z, np.arange(nt, dtype=float), np.arange(nf, dtype=float), max_cells)
    assert out.shape[0] == len(t) and out.shape[1] == len(f)
    assert out.size <= max(max_cells, 1)
    assert np.iscomplexobj(out)


def test_decimation_preserves_the_mean() -> None:
    z = np.ones((64, 128), dtype=complex) * (2 + 3j)
    out, t, f = vis_ops.decimate_cube(
        z, np.arange(64, dtype=float), np.arange(128, dtype=float), 100
    )
    assert out == pytest.approx(np.full(out.shape, 2 + 3j))
    assert t.size * f.size <= 100
