"""SNAPs tab API shapes, against a seeded tmp_path store and fake layout CSVs."""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from casm_monitor.collectors import rowmap
from casm_monitor.collectors.kafka_bp import NCHAN, latest_frame_path
from casm_monitor.store import ShardWriter, Store
from casm_monitor.web import snaps as snaps_module
from casm_monitor.web.app import create_app

LAYOUT = (
    "antenna,snap,adc,packet_idx,functional,include_in_beamforming,row,col,snap_ip,slot\n"
    "1,0,0,0,1,1,N21,E1,192.168.120.52,A\n"
    "2,0,1,1,0,0,,,,\n"
    "3,0,2,2,1,0,N21,E4,192.168.120.52,A\n"
    "13,1,0,12,1,1,N11,E1,192.168.120.51,I\n"
)
SNAP_MAP = "chassis,slot,feng_id,snap_ip\n1,A,0,192.168.120.52\n1,I,1,192.168.120.51\n"


@pytest.fixture
def layouts(tmp_path, monkeypatch):
    layout = tmp_path / "layout.csv"
    snap_map = tmp_path / "snap_map.csv"
    layout.write_text(LAYOUT)
    snap_map.write_text(SNAP_MAP)
    monkeypatch.setattr(rowmap, "LAYOUT_CSV", layout)
    monkeypatch.setattr(rowmap, "SNAP_MAP_CSV", snap_map)
    return layout, snap_map


def seed(settings, *, with_frame=True, with_history=True) -> None:
    store = Store(settings.db_path, store_root=settings.store_root)
    shards = ShardWriter(store, settings.shards_root)
    # Wired inputs per LAYOUT are packet_idx 0, 2, 12 (functional=1), whose
    # formula rows are 0, 4, 24 (row = 2 * packet_idx). Row 0 validated clean,
    # row 4 flagged by a validation disagreement, row 24 never validated.
    rowmap.save_row_map(
        store,
        {
            0: {"packet_idx": 0, "corr": 0.99, "runner_up": 0.5, "status": "formula+verified"},
            4: {"packet_idx": 2, "corr": 0.4, "runner_up": 0.91, "status": "mismatch"},
        },
        {"obs": "2026-09-04-16:43:47", "file_index": 82},
        ts=time.time(),
    )
    now = time.time()
    if with_frame:
        path = latest_frame_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(
            path,
            ts=np.float64(now - 8.0),
            written=np.float64(now),
            rows=np.asarray([0, 2, 4], dtype=np.int32),
            subbands_ok=np.asarray([True, True, True, False, True, True]),
            power_db=np.tile(np.linspace(0.0, 10.0, NCHAN, dtype=np.float32), (3, 1)),
        )
    if with_history:
        times = [now - 3600.0 + 60.0 * i for i in range(20)]
        array = np.stack(
            [np.full((3, NCHAN), float(i), dtype=np.float32) for i in range(len(times))]
        )
        shards.write(
            "kafka_bp_full",
            array,
            t0=times[0],
            t1=times[-1],
            meta={"rows": [0, 2, 4], "t": times, "units": "dB", "chan_avg": 1},
        )
        for i, t in enumerate(times):
            store.put_scalar(
                "kafka_bp.row0.power_db", -3.0 + 0.01 * i, tags={"row": 0, "packet_idx": 0}, ts=t
            )
        store.put_scalar(
            "snaps.night_median_db", -4.2, tags={"row": 0, "packet_idx": 0, "date": "2026-09-08"},
            ts=times[0] + 30.0,
        )
        store.add_event("obs_restart", subject="observation", ts=times[0] + 10.0)
    store.close()


@contextlib.contextmanager
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def test_boards_shape(settings, layouts):
    seed(settings, with_frame=False, with_history=False)
    with client(settings) as c:
        body = c.get("/api/snaps/boards").json()
    boards = body["boards"]
    antenna = [b for b in boards if b["role"] == "antenna"]
    relays = [b for b in boards if b["role"] == "relay"]
    assert [b["ip"] for b in antenna] == ["192.168.120.52", "192.168.120.51"]
    assert [b["feng_id"] for b in antenna] == [0, 1]
    assert antenna[0]["slot"] == "A"
    assert len(antenna[0]["inputs"]) == 12  # always twelve, in ADC order
    assert [i["adc"] for i in antenna[0]["inputs"]] == list(range(12))
    first = antenna[0]["inputs"][0]
    assert (first["packet_idx"], first["antenna"], first["station"]) == (0, 1, "N21E1")
    assert first["in_bf"] is True and first["functional"] is True
    assert antenna[0]["inputs"][1]["functional"] is False
    assert antenna[0]["inputs"][5]["packet_idx"] is None  # no layout row for adc 5
    assert [b["ip"] for b in relays] == list(rowmap.RELAY_IPS)
    assert all("inputs" not in b for b in relays)


def test_live_returns_mapped_inputs(settings, layouts):
    seed(settings)
    with client(settings) as c:
        body = c.get("/api/snaps/live?ip=192.168.120.52&nchan=768").json()
    assert body["subbands_ok"] == [True, True, True, False, True, True]
    assert 0 < body["age_s"] < 3600
    assert len(body["freq_mhz"]) == 768
    assert body["freq_mhz"][0] > body["freq_mhz"][-1]  # descending
    assert body["eq_epoch"] is None
    by_adc = {i["adc"]: i for i in body["inputs"]}
    assert by_adc[0]["mapping"] == "formula+verified"
    assert len(by_adc[0]["bp"]) == 768
    # A validation "mismatch" is a flag, not a rejection: the tile still gets
    # its formula row's data.
    assert by_adc[2]["mapping"] == "mismatch"
    assert len(by_adc[2]["bp"]) == 768
    # packet_idx 1 (adc 1) is wired=false: no formula row at all.
    assert by_adc[1]["mapping"] == "unmapped"
    assert by_adc[1]["bp"] is None


def test_live_without_a_frame_is_an_empty_state_not_a_404(settings, layouts):
    seed(settings, with_frame=False, with_history=False)
    with client(settings) as c:
        response = c.get("/api/snaps/live?ip=192.168.120.52")
    body = response.json()
    assert response.status_code == 200
    assert body["ts"] is None and body["age_s"] is None
    assert all(i["bp"] is None for i in body["inputs"])


def test_live_unknown_board_is_404(settings, layouts):
    seed(settings, with_frame=False, with_history=False)
    with client(settings) as c:
        assert c.get("/api/snaps/live?ip=10.0.0.1").status_code == 404


def test_history_respects_max_cells(settings, layouts):
    seed(settings)
    t0 = time.time() - 7200.0
    with client(settings) as c:
        body = c.get(f"/api/snaps/history?packet_idx=0&max_cells=5000&t0={t0}").json()
    assert body["res"] in ("10s", "60s", "10min", "1h")
    assert len(body["t"]) * len(body["freq_mhz"]) <= 5000
    assert len(body["z_db"]) == len(body["t"])
    assert all(len(line) == len(body["freq_mhz"]) for line in body["z_db"])
    assert body["freq_mhz"][0] > body["freq_mhz"][-1]
    assert body["t"][0] < body["t"][-1]


def test_history_unmapped_input_is_404(settings, layouts):
    seed(settings)
    with client(settings) as c:
        # packet_idx 1 is not functional (not wired), so it has no formula row.
        assert c.get("/api/snaps/history?packet_idx=1").status_code == 404


def test_history_picks_the_subband_stream_for_a_long_span(settings, layouts):
    seed(settings)
    with client(settings) as c:
        body = c.get("/api/snaps/history?packet_idx=0&t0=0").json()
    assert body["stream"] == "kafka_bp_sub"  # span > 2 d


def test_history_empty_window_is_not_an_error(settings, layouts):
    seed(settings)
    with client(settings) as c:
        body = c.get("/api/snaps/history?packet_idx=0&t0=1000&t1=2000").json()
    assert body["t"] == [] and body["z_db"] == []


def test_trend_shapes(settings, layouts):
    seed(settings)
    with client(settings) as c:
        body = c.get("/api/snaps/trend?packet_idx=0&t0=0").json()
    assert len(body["t"]) == len(body["band_power_db"]) == len(body["night_median_db"])
    assert body["night_median_db"][-1] == pytest.approx(-4.2)
    assert [e["kind"] for e in body["epochs"]] == ["obs_restart"]


def test_mapping_route_reports_scores(settings, layouts):
    seed(settings, with_frame=False, with_history=False)
    with client(settings) as c:
        body = c.get("/api/snaps/mapping").json()
    assert body["mapping"]["0"]["packet_idx"] == 0
    assert body["mapping"]["0"]["status"] == "formula+verified"
    assert body["mapping"]["4"]["packet_idx"] == 2
    assert body["mapping"]["4"]["status"] == "mismatch"
    # packet_idx 12's formula row (24) is wired but never validated.
    assert body["mapping"]["24"]["packet_idx"] == 12
    assert body["mapping"]["24"]["status"] == "formula"
    assert body["source"]["file_index"] == 82


def test_board_read_stub_answers_501(settings, layouts):
    """Our placeholder (used only while the snapread half is absent) is a clean
    501, never a 404, and never shadows the real routes once they exist."""
    from fastapi import FastAPI

    seed(settings, with_frame=False, with_history=False)
    store = Store(settings.db_path, read_only=True, store_root=settings.store_root)
    app = FastAPI()
    app.include_router(snaps_module.build_router(settings, store, board_read_stub=True))
    with TestClient(app) as c:
        assert c.get("/api/snaps/board-read?ip=192.168.120.52").status_code == 501
        assert c.post("/api/snaps/board-read", json={"ips": None}).status_code == 501
    store.close()
    # With the real module present the stub must not be registered at all.
    app2 = FastAPI()
    app2.include_router(
        snaps_module.build_router(settings, Store(settings.db_path, read_only=True,
                                                 store_root=settings.store_root),
                                 board_read_stub=False)
    )
    paths = {r.path for r in app2.routes}
    assert "/api/snaps/board-read" not in paths


def test_decimation_helpers():
    z = np.zeros((100, 3072), dtype=np.float32)
    t = np.arange(100, dtype=np.float64)
    freq = rowmap.freq_axis_mhz()
    zz, tt, ff = snaps_module.decimate(z, t, freq, 10_000)
    assert zz.shape == (len(tt), len(ff))
    assert len(tt) * len(ff) <= 10_000
    assert snaps_module.average_channels(freq, 96).size == 96
    assert snaps_module.average_channels(freq, 5000).size == 3072


def test_decimation_is_strict_about_max_cells():
    """Including the time-heavy case, where frequency has to collapse."""
    freq = rowmap.freq_axis_mhz()
    for nt, nf, budget in ((100, 3072, 10_000), (50_000, 3072, 200), (7, 3072, 100)):
        z = np.zeros((nt, nf), dtype=np.float32)
        t = np.arange(nt, dtype=np.float64)
        zz, tt, ff = snaps_module.decimate(z, t, freq[:nf], budget)
        assert zz.shape == (len(tt), len(ff))
        assert len(tt) * len(ff) <= budget, (nt, nf, budget, zz.shape)
        assert len(tt) >= 1 and len(ff) >= 1


def test_decimation_averages_in_linear_power_not_in_db():
    """Two channels at 0 dB and 20 dB average to 10log10(50.5) = 17.0 dB, not
    to the arithmetic mean of the dB values (10 dB)."""
    z = np.array([[0.0, 20.0]], dtype=np.float64)
    out = snaps_module.average_db(z, 1)
    assert out[0, 0] == pytest.approx(10.0 * np.log10(50.5), abs=1e-6)
    assert out[0, 0] != pytest.approx(10.0)

    # Same in the time direction.
    column = np.array([[0.0], [20.0]], dtype=np.float64)
    assert snaps_module.average_db_axis0(column, 1)[0, 0] == pytest.approx(
        10.0 * np.log10(50.5), abs=1e-6
    )

    # And through decimate(): a 2x2 of 0/20 dB reduced to one cell.
    z4 = np.array([[0.0, 20.0], [0.0, 20.0]], dtype=np.float64)
    zz, _tt, ff = snaps_module.decimate(z4, np.arange(2.0), np.array([1.0, 2.0]), 1)
    assert zz.shape == (1, 1) and len(ff) == 1
    assert zz[0, 0] == pytest.approx(10.0 * np.log10(50.5), abs=1e-6)


def test_history_small_max_cells_is_not_raised_to_1000(settings, layouts):
    seed(settings)
    t0 = time.time() - 7200.0
    with client(settings) as c:
        body = c.get(f"/api/snaps/history?packet_idx=0&max_cells=200&t0={t0}").json()
    assert 0 < len(body["t"]) * len(body["freq_mhz"]) <= 200
    # Absurd requests are clamped to the documented bounds, not honoured.
    with client(settings) as c:
        huge = c.get(f"/api/snaps/history?packet_idx=0&max_cells=99999999&t0={t0}").json()
    assert len(huge["t"]) * len(huge["freq_mhz"]) <= snaps_module.MAX_MAX_CELLS
