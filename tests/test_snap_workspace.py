"""SNAP adapter reuses history, preflights decoded manifests and saves evidence."""
from pathlib import Path

import numpy as np
import pytest
from fastapi import APIRouter, HTTPException

from casm_monitor.config import Settings
from casm_monitor.store import Store
from casm_monitor.web import snap_workspace as module


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    settings = Settings(store_root=tmp_path / "store", observation_cache_root=tmp_path / "preview")
    store = Store(settings.db_path, store_root=settings.store_root)
    calls = []
    source = {"t": [100.0, 160.0], "freq_mhz": [430.0, 420.0, 410.0],
              "z_db": [[0.0, 10.0, 20.0], [10.0, 20.0, 30.0]],
              "stream": module.STREAM_FULL, "res": "60s", "n_samples_raw": 2}

    def fake_router(*args):
        router = APIRouter()

        @router.get("/api/snaps/history")
        def history(**kwargs):
            calls.append(kwargs)
            return source

        return router

    monkeypatch.setattr(module, "snap_router", fake_router)
    import casm_vis_analysis.solar_waterfall as waterfall

    def plot(power, times, freq, path=None, **kwargs):
        calls.append({"power": power, "times": times, "freq": freq, **kwargs})
        if path:
            Path(path).write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        else:
            from matplotlib.figure import Figure
            return Figure()

    monkeypatch.setattr(waterfall, "plot_dynamic_spectrum", plot)
    router = module.build_router(settings, store)
    render = next(r.endpoint for r in router.routes if r.path.endswith("/render"))
    artifact = next(r.endpoint for r in router.routes if r.path.endswith("/{filename}"))
    yield settings, store, calls, source, render, artifact
    store.close()


def register(store, shape):
    return store.register_shard(stream=module.STREAM_FULL, t0=100, t1=160,
                                path=str(store.store_root / "unused.zarr"), dtype="float32",
                                shape=shape, meta={"t": [100, 160], "rows": [0]})


def selection(**kwargs):
    return module.Selection(packet_idx=0, t0="90", t1="170", **kwargs)


def test_reuses_history_and_power_conversion(workspace):
    settings, store, calls, source, render, artifact = workspace
    shard_id = register(store, [2, 1, 3])
    output = render(selection(fmin=415, fmax=435))
    assert output["provenance"]["estimated_read_bytes"] == 24
    assert output["provenance"]["shard_ids"] == [shard_id]
    assert output["provenance"]["display_sampling_width_s"] == 60
    assert output["provenance"]["physical_integration_s"] is None
    assert calls[0]["source"] == "kafka"
    np.testing.assert_array_equal(calls[1]["freq"], [430, 420])
    np.testing.assert_allclose(calls[1]["power"], [[1, 10], [10, 100]])
    assert calls[1]["integration_s"] == 60
    before = len(calls)
    assert render(selection(fmin=415, fmax=435))["id"] == output["id"]
    assert len(calls) == before
    assert artifact(output["id"], "spectrum.png").path.is_file()
    with pytest.raises(HTTPException) as exc:
        artifact(output["id"], "../../outside")
    assert exc.value.status_code == 404


def test_preflight_refuses_large_manifest_before_history(workspace):
    settings, store, calls, source, render, artifact = workspace
    register(store, [1000, 48, 3072])
    with pytest.raises(HTTPException) as exc:
        render(selection())
    assert exc.value.status_code == 400 and "256 MiB" in exc.value.detail
    assert calls == []


def test_preflight_does_not_overflow_machine_integer(workspace):
    settings, store, calls, source, render, artifact = workspace
    register(store, [2**40, 2**40, 1])
    with pytest.raises(HTTPException) as exc:
        render(selection())
    assert exc.value.status_code == 400 and "256 MiB" in exc.value.detail
    assert calls == []


def test_missing_history_is_not_zero_signal(workspace):
    settings, store, calls, source, render, artifact = workspace
    source["t"] = []
    with pytest.raises(HTTPException) as exc:
        render(selection())
    assert exc.value.status_code == 404


def test_bad_interval_and_frequency_selection(workspace):
    settings, store, calls, source, render, artifact = workspace
    with pytest.raises(HTTPException) as exc:
        render(module.Selection(packet_idx=0, t0="nan", t1="170"))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        render(selection(fmin=410, fmax=410))
    assert exc.value.status_code == 400
    assert calls == []


def test_time_averaging_and_duplicate_timestamps_are_refused(workspace):
    settings, store, calls, source, render, artifact = workspace
    source["n_samples_raw"] = 8
    with pytest.raises(HTTPException) as exc:
        render(selection())
    assert exc.value.status_code == 400 and "averaging" in exc.value.detail
    source["n_samples_raw"] = 2
    source["t"] = [100, 100]
    with pytest.raises(HTTPException) as exc:
        render(selection())
    assert exc.value.status_code == 409
