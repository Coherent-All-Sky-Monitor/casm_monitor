"""Web API shape against a seeded store."""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from casm_monitor.store import Store
from casm_monitor.util import iso
from casm_monitor.web import app as app_module
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


@contextlib.contextmanager
def make_client(settings):
    """A TestClient entered as a context manager, so FastAPI's lifespan
    (startup/shutdown, including our WS-task bookkeeping) actually runs."""
    store = Store(settings.db_path, store_root=settings.store_root)
    seed(store)
    store.close()
    with TestClient(create_app(settings)) as client:
        yield client


def test_health(settings):
    with make_client(settings) as client:
        body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["version"]
    assert 0 <= body["collect_age_s"] < 60


def test_status_shape_and_grading(settings):
    with make_client(settings) as client:
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
    assert items["cas_a_alt"]["ts"] is None
    assert items["cas_a_alt"]["age_s"] is None
    # every group is non-empty and every item belongs to exactly one group
    keys = {k for g in body["groups"] for k in g["keys"]}
    assert keys == set(items)
    assert all(g["keys"] for g in body["groups"])


def test_events_filters(settings):
    with make_client(settings) as client:
        body = client.get("/api/events").json()
        assert [e["kind"] for e in body["events"]] == ["collector_failing", "obs_restart"]
        assert body["events"][0]["ts"].endswith("Z")
        assert set(body["events"][0]) == {"id", "ts", "kind", "severity", "subject", "detail"}
        assert len(client.get("/api/events?severity=error").json()["events"]) == 1
        assert len(client.get("/api/events?kind=obs_restart").json()["events"]) == 1
        # kind is a prefix/LIKE match
        assert len(client.get("/api/events?kind=obs_").json()["events"]) == 1
        assert len(client.get("/api/events?kind=collector").json()["events"]) == 1
        assert len(client.get("/api/events?kind=nope").json()["events"]) == 0
        assert client.get("/api/events?severity=bogus").status_code == 400
        # ISO strings are second-resolution, so ask from one second in the future
        future = iso(time.time() + 1.0)
        assert client.get(f"/api/events?since={future}").json()["events"] == []
        # an unparseable since is a client error, not "ignore the filter"
        bad = client.get("/api/events?since=not-a-timestamp")
        assert bad.status_code == 400


def test_scalars_series(settings):
    with make_client(settings) as client:
        body = client.get("/api/scalars?name=gpu.util_max_pct&max_points=10").json()
        assert body["name"] == "gpu.util_max_pct"
        # n=50, max_points=10 -> stride=5: rn 1,6,...,46 (1-indexed), plus the
        # forced last point (rn=50). Values are float(i) for i=rn-1.
        expected_v = [0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0, 45.0, 49.0]
        assert body["v"] == expected_v
        assert len(body["t"]) == len(body["v"]) == 11
        assert body["t"] == sorted(body["t"])  # endpoints in order, ts ascending

        empty = client.get("/api/scalars?name=nope").json()
        assert empty["t"] == [] and empty["v"] == []

        bad = client.get("/api/scalars?name=gpu.util_max_pct&t0=not-a-timestamp")
        assert bad.status_code == 400
        bad2 = client.get("/api/scalars?name=gpu.util_max_pct&t1=not-a-timestamp")
        assert bad2.status_code == 400


def test_websocket_pushes_status(settings):
    with make_client(settings) as client:
        with client.websocket_connect("/ws/status") as ws:
            payload = ws.receive_json()
        assert "items" in payload and "groups" in payload


def test_spa_fallback_and_api_404(settings):
    """Unknown non-/api paths fall back to index.html (built SPA or placeholder)."""
    with make_client(settings) as client:
        root = client.get("/")
        assert root.status_code == 200 and "<html" in root.text.lower()
        deep = client.get("/snaps/board/52")
        assert deep.status_code == 200 and deep.text == root.text
        assert client.get("/api/does-not-exist").status_code == 404
        assert client.get("/api/does-not-exist").json() == {"detail": "not found"}


def test_spa_serves_real_file_from_static_dir(settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>built spa</body></html>")
    assets_dir = static_dir / "assets"
    assets_dir.mkdir()
    (assets_dir / "app.js").write_text("console.log('hi');")

    monkeypatch.setattr(app_module, "STATIC_DIR", static_dir)
    with make_client(settings) as client:
        root = client.get("/")
        assert root.status_code == 200 and "built spa" in root.text

        asset = client.get("/assets/app.js")
        assert asset.status_code == 200
        assert "console.log" in asset.text

        # unknown route still falls back to index.html, not a 404
        unknown = client.get("/some/client/route")
        assert unknown.status_code == 200 and "built spa" in unknown.text


def test_spa_path_traversal_cannot_escape_static_dir(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>built spa</body></html>")
    secret = tmp_path / "etc_passwd_stand_in"
    secret.write_text("root:x:0:0::/root:/bin/bash\n")

    monkeypatch.setattr(app_module, "STATIC_DIR", static_dir)
    with make_client(settings) as client:
        for path in ("/../../etc_passwd_stand_in", "//etc_passwd_stand_in", "/..%2f..%2fetc_passwd_stand_in"):
            resp = client.get(path)
            # Never the secret file's contents; either normalized back inside
            # the static dir (index.html fallback, 200) or rejected outright.
            assert "root:x:0:0" not in resp.text
            if resp.status_code == 200:
                assert "built spa" in resp.text


def test_bad_since_and_t0_are_400_not_ignored(settings):
    with make_client(settings) as client:
        assert client.get("/api/events?since=garbage").status_code == 400
        assert client.get("/api/scalars?name=gpu.util_max_pct&t0=garbage").status_code == 400
        # empty string is "no filter", not an error
        assert client.get("/api/events?since=").status_code == 200
