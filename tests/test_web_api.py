"""Web API shape against a seeded store."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from casm_monitor.store import Store
from casm_monitor.util import iso
from casm_monitor.web.app import create_app
from casm_monitor.web.status import GROUP_ORDER


def seed(store: Store) -> None:
    now = time.time()
    store.put_scalar("obs.utc_start", "2026-09-04-16:43:47", ts=now)
    store.put_scalar("obs.daemons_state", "running", ts=now)
    store.put_scalar("obs.lmc_ok", 1, ts=now)
    store.put_scalar("obs.sub_incoh", 1, ts=now)
    store.put_scalar("weights.product_id", "be96b97040374b78", ts=now)
    store.put_scalar("weights.registry_mismatch", 1, ts=now)
    store.put_scalar("hella.corr1.snr", 15.0, ts=now)
    store.put_scalar("services.redis_ok", 0, ts=now)
    store.put_scalar("services.kafka_ok", 1, ts=now)
    store.put_scalar("sky.sun.alt_deg", 41.2, ts=now)
    store.put_scalar("disk.mnt_nvme5.pct_used", 97.0, ts=now)
    store.put_scalar("store.bytes", 4096, ts=now)
    # a deliberately old scalar: 300 s at a 30 s cadence -> stale
    store.put_scalar("obs.bf_scale_factor", 8.0, ts=now - 300.0)
    # and one 70 s old at a 30 s cadence -> warn
    store.put_scalar("obs.daemons_up", 22, ts=now - 70.0)
    store.add_event("obs_restart", subject="observation", detail={"to": "x"}, ts=now - 10)
    store.add_event("collector_failing", severity="error", subject="gpus", ts=now - 5)
    store.heartbeat_ok("obs", ts=now - 3)
    for i in range(50):
        store.put_scalar("gpu.util_max_pct", float(i), ts=now - 100 + i)


def make_client(settings) -> TestClient:
    store = Store(settings.db_path, store_root=settings.store_root)
    seed(store)
    store.close()
    return TestClient(create_app(settings))


def test_health(settings):
    client = make_client(settings)
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["version"]
    assert 0 <= body["collect_age_s"] < 60


def test_status_shape_and_grading(settings):
    client = make_client(settings)
    body = client.get("/api/status").json()
    assert body["ts"].endswith("Z")
    assert [g["name"] for g in body["groups"]] == list(GROUP_ORDER)
    items = body["items"]
    for key, item in items.items():
        assert set(item) == {"value", "ts", "age_s", "state", "label", "unit", "group"}
        assert item["state"] in ("ok", "warn", "stale", "error")
        assert item["label"]
        assert item["group"] in GROUP_ORDER

    assert items["obs_utc_start"]["value"] == "2026-09-04-16:43:47"
    assert items["obs_utc_start"]["state"] == "ok"
    assert items["redis_ok"]["state"] == "error"  # value 0 -> dependency down
    assert items["weights_registry_mismatch"]["state"] == "warn"
    assert items["nvme5_pct_used"]["state"] == "warn"  # 97% > 90%
    assert items["obs_daemons_up"]["state"] == "warn"  # 70 s at 30 s cadence
    assert items["obs_bf_scale_factor"]["state"] == "stale"  # 300 s at 30 s cadence
    assert items["cas_a_alt"]["value"] is None  # never collected
    assert items["cas_a_alt"]["state"] == "stale"
    # every group is non-empty and every item belongs to exactly one group
    keys = {k for g in body["groups"] for k in g["keys"]}
    assert keys == set(items)
    assert all(g["keys"] for g in body["groups"])


def test_events_filters(settings):
    client = make_client(settings)
    body = client.get("/api/events").json()
    assert [e["kind"] for e in body["events"]] == ["collector_failing", "obs_restart"]
    assert body["events"][0]["ts"].endswith("Z")
    assert set(body["events"][0]) == {"id", "ts", "kind", "severity", "subject", "detail"}
    assert len(client.get("/api/events?severity=error").json()["events"]) == 1
    assert len(client.get("/api/events?kind=obs_restart").json()["events"]) == 1
    assert client.get("/api/events?severity=bogus").status_code == 400
    # ISO strings are second-resolution, so ask from one second in the future
    future = iso(time.time() + 1.0)
    assert client.get(f"/api/events?since={future}").json()["events"] == []


def test_scalars_series(settings):
    client = make_client(settings)
    body = client.get("/api/scalars?name=gpu.util_max_pct&max_points=10").json()
    assert body["name"] == "gpu.util_max_pct"
    assert len(body["t"]) == len(body["v"]) <= 11
    assert body["v"][-1] == 49.0
    empty = client.get("/api/scalars?name=nope").json()
    assert empty["t"] == [] and empty["v"] == []


def test_websocket_pushes_status(settings):
    client = make_client(settings)
    with client.websocket_connect("/ws/status") as ws:
        payload = ws.receive_json()
    assert "items" in payload and "groups" in payload


def test_spa_fallback_and_api_404(settings):
    """Unknown non-/api paths fall back to index.html (built SPA or placeholder)."""
    client = make_client(settings)
    root = client.get("/")
    assert root.status_code == 200 and "<html" in root.text.lower()
    deep = client.get("/snaps/board/52")
    assert deep.status_code == 200 and deep.text == root.text
    assert client.get("/api/does-not-exist").status_code == 404
    assert client.get("/api/does-not-exist").json() == {"detail": "not found"}
