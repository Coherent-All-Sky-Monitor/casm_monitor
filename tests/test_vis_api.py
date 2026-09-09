"""Visibilities API shapes, against a seeded tmp_path store and a fake layout.

No /mnt read: the shards and the latest-integration mirror are synthetic, the
layout is a four-row CSV, and the cal reference is a stub in place of the
deployed HDF5.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from casm_monitor.collectors import rowmap
from casm_monitor.collectors import vis as visc
from casm_monitor.store import ShardWriter, Store
from casm_monitor.web import vis as webvis
from casm_monitor.web.app import create_app

OBS = "2026-09-04-16:43:47"
LAYOUT = (
    "antenna,x,y,z,snap,adc,packet_idx,functional,include_in_beamforming,row,col\n"
    "1,0.0,0.0,0.0,0,0,0,1,0,N01,E1\n"
    "2,0.0,0.0,0.0,0,1,1,0,0,,\n"
    "3,120.0,0.0,0.1,0,2,2,1,1,N01,E3\n"
    "4,0.0,90.0,0.2,0,3,3,1,1,N02,E1\n"
)
INPUTS = [0, 2, 3]
N_BL = 6
T0 = 1788899767.0
NCHAN = 3072


@pytest.fixture
def layout(tmp_path, monkeypatch):
    path = tmp_path / "layout.csv"
    path.write_text(LAYOUT)
    monkeypatch.setattr(rowmap, "LAYOUT_CSV", path)
    webvis._positions_cache.clear()
    return path


def _frame(scale: float = 1.0) -> np.ndarray:
    """(n_bl, NCHAN) with real positive autos and a fringing cross."""
    from casm_io.correlator.baselines import triu_flat_index

    freq = rowmap.freq_axis_mhz()
    arr = np.zeros((N_BL, NCHAN), dtype=np.complex64)
    for k in range(len(INPUTS)):
        arr[triu_flat_index(len(INPUTS), k, k)] = 1e6 * (k + 1) * scale
    for a in range(len(INPUTS)):
        for b in range(a + 1, len(INPUTS)):
            idx = triu_flat_index(len(INPUTS), a, b)
            arr[idx] = 1e5 * scale * np.exp(2j * np.pi * freq / 7.0 * (b - a))
    return arr


@pytest.fixture
def seeded(settings, layout):
    """A store with 16 vis_full shards, 2 vis_avg8 shards and a mirror."""
    store = Store(settings.db_path, store_root=settings.store_root)
    shards = ShardWriter(store, settings.shards_root)
    meta_common = {
        "obs": OBS,
        "inputs": INPUTS,
        "freq_top_mhz": rowmap.FREQ_TOP_MHZ,
        "freq_order": "descending",
    }
    times = [T0 + k * visc.DT_S for k in range(16)]
    for k, ts in enumerate(times):
        shards.write(
            visc.STREAM_FULL,
            _frame(1.0 + 0.01 * k),
            t0=ts,
            t1=ts,
            meta={
                **meta_common,
                "t": [ts],
                "chan_avg": 1,
                "chan_bw_mhz": rowmap.CHAN_BW_MHZ,
                "file_idx": 0,
                "int_idx": k,
                "flags": {"first_of_file": k == 0},
            },
            chunks=(1, NCHAN),
        )
    for block in range(2):
        group = times[block * 8: (block + 1) * 8]
        arr = np.stack(
            [
                _frame(1.0 + 0.01 * (block * 8 + i))
                .reshape(N_BL, visc.AVG_NCHAN, visc.CHAN_AVG)
                .mean(axis=2)
                for i in range(len(group))
            ]
        ).astype(np.complex64)
        shards.write(
            visc.STREAM_AVG8,
            arr,
            t0=group[0],
            t1=group[-1],
            meta={
                **meta_common,
                "t": group,
                "chan_avg": visc.CHAN_AVG,
                "chan_bw_mhz": rowmap.CHAN_BW_MHZ * visc.CHAN_AVG,
                "flags": {"first_of_file": [False] * len(group)},
            },
            chunks=(1, N_BL, visc.AVG_NCHAN),
        )
    visc.write_latest_vis(
        settings,
        vis=_frame(1.15),
        inputs=INPUTS,
        freq_mhz=rowmap.freq_axis_mhz(),
        ts=times[-1],
        obs=OBS,
        file_idx=0,
        int_idx=15,
        first_of_file=False,
    )
    store.close()
    return times


@pytest.fixture
def client(settings, seeded):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _fake_cal(n_ant=4, nchan=NCHAN):
    rng = np.random.default_rng(11)
    gains = np.exp(1j * rng.uniform(-np.pi, np.pi, size=(n_ant, nchan)))
    return SimpleNamespace(
        weights=np.conj(gains),
        flags=np.ones(nchan, dtype=bool),
        frequencies_hz=np.sort(rowmap.freq_axis_mhz()) * 1e6,
        ant_ids=np.arange(1, n_ant + 1),
        ref_ant_id=1,
        source="sun",
    )


# -- inputs / times -----------------------------------------------------
def test_inputs(client, seeded) -> None:
    body = client.get("/api/vis/inputs").json()
    assert body["sets"] == {"live": [2, 3], "wired": [0, 2, 3]}
    assert [row["packet_idx"] for row in body["inputs"]] == INPUTS
    assert body["obs"]["utc_start"] == OBS
    assert body["obs"]["n_cached"] == 16
    assert body["obs"]["latest_ts"] == pytest.approx(seeded[-1])
    assert body["obs"]["oldest_ts"] == pytest.approx(seeded[0])
    assert body["obs"]["n_cached_avg8"] == 2
    assert set(body["units"]) == set(body["quantities"])


def test_times(client, seeded) -> None:
    body = client.get(f"/api/vis/times?t0={seeded[0] - 1}&t1={seeded[-1] + 1}").json()
    assert body["t"] == pytest.approx(seeded)
    assert body["n"] == 16
    narrow = client.get(f"/api/vis/times?t0={seeded[4]}&t1={seeded[6]}").json()
    assert narrow["t"] == pytest.approx(seeded[4:7])
    assert client.get("/api/vis/times?t0=notatime").status_code == 400


# -- spectra ------------------------------------------------------------
def test_spectra_autos_latest(client) -> None:
    body = client.get(
        "/api/vis/spectra?ts=latest&set=live&pairs=auto&quantity=amp&units=db&nchan=96"
    ).json()
    assert body["quantity"] == "amp" and body["units"] == "db" and body["ref"] == "raw"
    assert body["nchan"] == 96
    assert len(body["freq_mhz"]) == 96
    assert [(b["i"], b["j"]) for b in body["baselines"]] == [(2, 2), (3, 3)]
    assert [(b["ant_i"], b["ant_j"]) for b in body["baselines"]] == [(3, 3), (4, 4)]
    assert len(body["baselines"][0]["y"]) == 96
    # input 2 is the second stored input: auto = 2e6 * 1.15
    assert body["baselines"][0]["y"][0] == pytest.approx(20 * np.log10(2e6 * 1.15), abs=0.01)
    assert body["flags"] == {"first_of_file": False}


def test_spectra_at_a_timestamp_and_pairs_modes(client, seeded) -> None:
    ts = seeded[3]
    cross = client.get(f"/api/vis/spectra?ts={ts}&set=wired&pairs=cross&nchan=32").json()
    assert cross["ts"] == pytest.approx(ts)
    assert [(b["i"], b["j"]) for b in cross["baselines"]] == [(0, 2), (0, 3), (2, 3)]
    every = client.get(f"/api/vis/spectra?ts={ts}&set=wired&pairs=all&nchan=32").json()
    assert len(every["baselines"]) == 6
    # a time with no cached integration is a 404, not a silent nearest match
    assert client.get(f"/api/vis/spectra?ts={seeded[-1] + 5 * visc.DT_S}").status_code == 404


def test_spectra_max_cells_lowers_nchan(client) -> None:
    body = client.get(
        "/api/vis/spectra?set=wired&pairs=all&nchan=3072&max_cells=1000"
    ).json()
    assert body["nchan"] * len(body["baselines"]) <= 1000
    # 1000 // 6 = 166 channels asked of 3072, so the block size lands on 162
    assert 150 <= body["nchan"] <= 1000 // 6


def test_spectra_coherence_and_phase(client) -> None:
    # Full resolution: |V_02| = 1e5 * scale, A_0 = 1e6 * scale, A_2 = 2e6 * scale,
    # and the scale cancels.
    coh = client.get("/api/vis/spectra?set=wired&pairs=cross&quantity=coh&nchan=3072").json()
    values = np.array(coh["baselines"][0]["y"])
    assert values == pytest.approx(np.full(3072, 1e5 / np.sqrt(1e6 * 2e6)), rel=1e-4)
    # Channel-averaged, the SAME baseline decorrelates: the fringe is averaged in
    # the complex plane, which is the point of the coherence view.
    averaged = client.get("/api/vis/spectra?set=wired&pairs=cross&quantity=coh&nchan=8").json()
    assert max(averaged["baselines"][0]["y"]) < values[0]
    phase = client.get(
        "/api/vis/spectra?set=wired&pairs=cross&quantity=phase&units=deg&nchan=3072"
    ).json()
    # np.angle is in (-pi, pi]; the bound is 180 deg up to the 5-digit rounding
    assert max(abs(v) for v in phase["baselines"][0]["y"]) <= 180.001


def test_spectra_ref_sun_and_cal(client, monkeypatch) -> None:
    sun = client.get("/api/vis/spectra?set=wired&pairs=cross&ref=sun&nchan=16").json()
    assert sun["ref_meta"]["ref"] == "sun" and sun["ref_meta"]["sign"] == -1
    assert isinstance(sun["ref_meta"]["sun_alt_deg"], float)
    # the frontend contract asks for this one in flags, not only in ref_meta
    assert sun["flags"]["sun_below_horizon"] == sun["ref_meta"]["sun_below_horizon"]
    assert sun["flags"]["first_of_file"] is False
    raw = client.get("/api/vis/spectra?set=wired&pairs=cross&ref=raw&nchan=16").json()
    # amplitude is untouched by fringe-stopping (a pure phase rotation) ...
    assert sun["baselines"][0]["y"] == pytest.approx(raw["baselines"][0]["y"], abs=1e-6)
    # ... but the phase is not
    sun_phase = client.get(
        "/api/vis/spectra?set=wired&pairs=cross&quantity=phase&ref=sun&nchan=16"
    ).json()
    raw_phase = client.get(
        "/api/vis/spectra?set=wired&pairs=cross&quantity=phase&ref=raw&nchan=16"
    ).json()
    assert sun_phase["baselines"][0]["y"] != raw_phase["baselines"][0]["y"]

    monkeypatch.setattr(
        webvis, "load_deployed_cal", lambda settings: (_fake_cal(), webvis.Path("/fake/cal.h5"))
    )
    cal = client.get("/api/vis/spectra?set=wired&pairs=cross&ref=cal&nchan=16").json()
    assert cal["ref_meta"]["cal_file"] == "cal.h5"
    assert cal["flags"]["cal_file"] == "cal.h5"
    assert cal["ref_meta"]["inputs_without_cal"] == []
    assert cal["baselines"][0]["y"] == pytest.approx(raw["baselines"][0]["y"], rel=1e-3)


def test_spectra_bad_params(client) -> None:
    for query in (
        "quantity=nope",
        "quantity=amp&units=deg",
        "quantity=coh&pairs=auto",
        "ref=moon",
        "set=everything",
        "pairs=weird",
        "nchan=0",
        "nchan=99999",
    ):
        assert client.get(f"/api/vis/spectra?{query}").status_code in (400, 422), query


# -- matrix -------------------------------------------------------------
def test_matrix_shape_and_symmetry(client) -> None:
    body = client.get("/api/vis/matrix?set=wired&quantity=amp&units=db").json()
    assert body["inputs"] == INPUTS
    assert body["antennas"] == [1, 3, 4]
    m = np.array(body["m"])
    assert m.shape == (3, 3)
    assert m == pytest.approx(m.T)  # amplitude is symmetric
    phase = np.array(client.get("/api/vis/matrix?set=wired&quantity=phase").json()["m"])
    assert phase == pytest.approx(-phase.T)  # V_ji = conj(V_ij)
    coh = np.array(client.get("/api/vis/matrix?set=wired&quantity=coh").json()["m"])
    assert np.diag(coh) == pytest.approx(np.ones(3))
    assert (coh <= 1.0 + 1e-9).all()


def test_matrix_band_limits(client) -> None:
    body = client.get("/api/vis/matrix?set=wired&quantity=coh&fmin=400&fmax=480").json()
    assert body["n_channels"] < NCHAN
    assert 400.0 <= body["band_mhz"][0] <= body["band_mhz"][1] <= 480.0
    assert client.get("/api/vis/matrix?fmin=480&fmax=400").status_code == 400
    assert client.get("/api/vis/matrix?fmin=600&fmax=700").status_code == 400


# -- waterfall ----------------------------------------------------------
def test_waterfall_full_res_and_decimation(client, seeded) -> None:
    body = client.get(
        f"/api/vis/waterfall?i=0&j=2&t0={seeded[0] - 1}&t1={seeded[-1] + 1}&quantity=amp&units=db"
    ).json()
    assert body["stream"] == visc.STREAM_FULL
    assert body["i"] == 0 and body["j"] == 2 and body["ant_i"] == 1 and body["ant_j"] == 3
    assert body["n_samples_raw"] == 16
    assert len(body["z"]) == len(body["t"])
    assert len(body["z"][0]) == len(body["freq_mhz"])
    assert len(body["t"]) * len(body["freq_mhz"]) <= webvis.DEFAULT_MAX_CELLS
    small = client.get(
        f"/api/vis/waterfall?i=0&j=2&t0={seeded[0] - 1}&t1={seeded[-1] + 1}&max_cells=1000"
    ).json()
    assert len(small["t"]) * len(small["freq_mhz"]) <= 1000
    # i/j order does not matter
    flipped = client.get(f"/api/vis/waterfall?i=2&j=0&t0={seeded[0] - 1}&t1={seeded[-1] + 1}").json()
    assert flipped["i"] == 0 and flipped["j"] == 2


def test_waterfall_long_span_uses_avg8(client, seeded) -> None:
    body = client.get(
        f"/api/vis/waterfall?i=0&j=3&t0={seeded[0] - 86400}&t1={seeded[-1]}"
    ).json()
    assert body["stream"] == visc.STREAM_AVG8
    assert len(body["freq_mhz"]) <= visc.AVG_NCHAN
    assert body["n_samples_raw"] == 16


def test_waterfall_quantities_and_refs(client, seeded) -> None:
    span = f"t0={seeded[0] - 1}&t1={seeded[-1] + 1}"
    coh = client.get(f"/api/vis/waterfall?i=0&j=2&{span}&quantity=coh").json()
    values = np.array(coh["z"], dtype=float)
    assert np.nanmax(values) <= 1.0 + 1e-6
    phase = client.get(f"/api/vis/waterfall?i=0&j=2&{span}&quantity=phase&units=rad&ref=sun").json()
    assert np.abs(np.array(phase["z"], dtype=float)).max() <= np.pi + 1e-9
    empty = client.get(f"/api/vis/waterfall?i=0&j=2&t0={seeded[0] - 7200}&t1={seeded[0] - 3600}").json()
    assert empty["z"] == [] and empty["t"] == []
    assert client.get(f"/api/vis/waterfall?i=0&j=2&{span}&quantity=amp&units=rad").status_code == 400


# -- coherence ----------------------------------------------------------
def test_coherence_matrix(client, seeded) -> None:
    body = client.get(
        f"/api/vis/coherence?t0={seeded[0] - 1}&t1={seeded[-1] + 1}&set=wired"
    ).json()
    assert body["inputs"] == INPUTS
    m = np.array(body["m"], dtype=float)
    assert m.shape == (3, 3)
    assert m == pytest.approx(m.T)
    assert np.diag(m) == pytest.approx(np.ones(3))
    assert body["n_samples"] == 16
    assert body["chan_avg"] == visc.CHAN_AVG
    # the default window (last night) holds nothing here, but must not fail
    default = client.get("/api/vis/coherence").json()
    assert len(default["m"]) == len(default["inputs"])


def test_coherence_rises_when_the_fringe_is_removed(client, seeded, settings) -> None:
    """A synthetic fringing baseline decorrelates in the band average; the same
    data fringe-stopped toward its own delay does not. Uses vis_ops directly so
    the check does not depend on where the Sun is at ``T0``."""
    from casm_monitor import vis_ops

    freq = rowmap.freq_axis_mhz()
    v = _frame()[1][None]  # the (0, 2) cross baseline
    raw = np.abs(v.mean(axis=1))[0]
    tau = np.array([[7.0 / 1e6 * 0 + 1.0 / (7.0 * 1e6)]])  # one cycle per 7 MHz
    stopped = np.swapaxes(
        vis_ops.fringe_stop_tfb(np.swapaxes(v[None], 1, 2), freq, tau, sign=-1), 1, 2
    )[0]
    assert np.abs(stopped.mean(axis=1))[0] > raw * 10


# -- read-only guarantee ------------------------------------------------
def test_no_route_writes_to_the_store(client, settings, seeded) -> None:
    before = sorted(p.name for p in settings.shards_root.rglob("*"))
    for path in (
        "/api/vis/inputs",
        "/api/vis/times",
        f"/api/vis/spectra?ts={seeded[-1]}",
        "/api/vis/matrix",
        f"/api/vis/waterfall?i=0&j=2&t0={seeded[0] - 1}&t1={seeded[-1] + 1}",
        "/api/vis/coherence",
    ):
        with contextlib.suppress(Exception):
            client.get(path)
    assert sorted(p.name for p in settings.shards_root.rglob("*")) == before
