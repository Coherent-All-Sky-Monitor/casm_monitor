"""Observation summaries distinguish wiring, intent and actual per-beam payload."""
import json
from dataclasses import replace

import h5py
import numpy as np
from fastapi.testclient import TestClient

from casm_monitor.figures.observation import inspect_membership, render_observation
from casm_monitor.observation import cache_dir
from casm_monitor.web.app import create_app
from casm_monitor.web.observation import build_observation


def product(path):
    with h5py.File(path, "w") as f:
        w = np.zeros((2, 65, 2, 3, 4), dtype=np.int8)
        w[0, 0, 0, 0, 0] = 1
        w[1, 64, 1, 1, 1] = -1  # last channel, imaginary/pol1 must count
        w[0, 10, 0, 2, :2] = 1
        f["weights_int8"] = w
        f["array_config/antenna_ids"] = [9, 19, 26, -1]
        f["array_config/positions_enu"] = np.zeros((4, 3))
        f["array_config/active_mask"] = [True, True, True, False]
        f.create_group("pointings").attrs["names"] = json.dumps(["a", "b", "c"])


def test_membership_reads_all_axes_not_global_mask(tmp_path):
    path = tmp_path / "weights.h5"
    product(path)
    result = inspect_membership(path)
    assert result["antennas"] == [9, 19]
    assert [b["antennas"] for b in result["beams"]] == [[9], [19], [9, 19]]
    assert result["global_metadata_antennas"] == [9, 19, 26]


def test_membership_budget_checked_before_payload_read(tmp_path, monkeypatch):
    from casm_monitor.figures import observation
    path = tmp_path / "weights.h5"
    product(path)
    monkeypatch.setattr(observation, "MAX_WEIGHT_BYTES", 1)
    import pytest
    with pytest.raises(ValueError, match="budget"):
        inspect_membership(path)


def isolated(settings, tmp_path):
    layout = tmp_path / "layout.csv"
    layout.write_text("antenna,x,y,z,snap,adc,packet_idx,functional,include_in_beamforming,row,col\n9,0,0,0,0,8,8,1,0,N01,E1\n19,1,2,0,1,6,18,1,1,N02,E1\n26,2,3,0,2,1,25,1,1,N03,E1\n")
    return replace(settings, snap_layout_csv=layout, registry_dir=tmp_path / "registry", t2_db=tmp_path / "missing.sqlite")


def test_overview_unknown_membership_never_falls_back_to_layout(settings, store, tmp_path):
    settings = isolated(settings, tmp_path)
    result = build_observation(settings, store, now=1789315200)
    assert result["layout"]["counts"] == {"wired": 3, "intended": 2, "deployed_union": None}
    assert all(p["deployed"] is None for p in result["layout"]["points"])
    assert result["solar"]["image_url"] is None
    assert result["clock"]["local"].endswith("-07:00")


def test_worker_caches_membership_and_get_does_not_open_hdf5(settings, store, tmp_path, monkeypatch):
    settings = isolated(settings, tmp_path)
    path = tmp_path / "weights.h5"
    product(path)
    products = settings.registry_dir / "products"
    products.mkdir(parents=True)
    (products / "fixture.json").write_text(json.dumps({"h5_path": str(path)}))
    (settings.registry_dir / "live_events.jsonl").write_text("\n".join(json.dumps({"stream": s, "product_id": "fixture", "utc": "2026-09-13T00:00:00Z"}) for s in range(6)))
    store.put_scalar("weights.product_id", "fixture")
    store.put_scalar("weights.product_source", "live_event")
    render_observation(store, settings, now=1789315200)
    def forbidden(*args, **kwargs):
        raise AssertionError("GET must not read weights payload")
    monkeypatch.setattr(h5py, "File", forbidden)
    result = build_observation(settings, store, now=1789315200)
    assert result["layout"]["counts"]["deployed_union"] == 2
    assert result["layout"]["counts"]["intended"] == 2
    points = {p["antenna"]: p for p in result["layout"]["points"]}
    assert points[9]["deployed"] is None and not points[9]["intended"]
    assert points[9]["slot_identity_matches"] is False  # fixture product uses different slots
    assert points[26]["intended"] and not points[26]["deployed"]
    # A new association must not inherit the previous product's members.
    store.put_scalar("weights.product_id", "different")
    assert build_observation(settings, store)["layout"]["counts"]["deployed_union"] is None


def test_stream_evidence_mixed_or_missing_is_not_one_deployment(settings, tmp_path):
    from casm_monitor.observation import recorded_product
    settings = isolated(settings, tmp_path)
    settings.registry_dir.mkdir()
    events = [{"stream": s, "product_id": "a", "utc": "2026-09-13T00:00:00Z"} for s in range(6)]
    events[-1]["product_id"] = "b"
    (settings.registry_dir / "live_events.jsonl").write_text("\n".join(map(json.dumps, events)))
    result = recorded_product(settings, {"weights.product_id": {"value": "a"}, "weights.product_source": {"value": "live_event"}})
    assert result["stream_evidence_state"] == "mixed_or_missing"
    assert result["path"] is None
    assert len(result["per_stream"]) == 6


def test_preview_refuses_posts_and_uses_readonly_store(settings, store, tmp_path):
    settings = isolated(settings, tmp_path)
    with TestClient(create_app(settings, read_only=True)) as client:
        assert client.post("/api/jobs", json={"kind": "render_figures"}).status_code == 403
        assert client.get("/api/observation").status_code == 200
        assert client.get("/api/observation/solar.png").status_code == 404
