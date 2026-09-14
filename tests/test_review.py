"""Preview-local records, explicit requests, pixel snapshots and CSRF guards."""
import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from casm_monitor.config import Settings
from casm_monitor.web import review


def client_for(tmp_path, monkeypatch, misses=(), workspace=True):
    monkeypatch.setenv("CASM_MONITOR_WORKSPACE", "1" if workspace else "0")
    settings = Settings(observation_cache_root=tmp_path / "preview", t2_db=tmp_path / "absent.sqlite")
    monkeypatch.setattr(review, "build_injection_overview", lambda s: {
        "misses": list(misses), "source": str(s.t2_db), "status": "ok",
        "counts_complete": True, "trend_start_utc": "2026-09-08T00:00:00Z"})
    app = FastAPI()
    app.include_router(review.build_router(settings))
    return TestClient(app), settings


def headers(client):
    return {"Origin": "http://testserver", "X-CASM-Review-CSRF": client.get("/api/review").json()["csrf_token"]}


def test_workspace_mirrors_miss_and_waits_for_human(tmp_path, monkeypatch):
    shot = {"file_id": "inj_20260913_0001", "outcome": "missed_t1", "inject_utc": "2026-09-13T00:00:00Z"}
    client, settings = client_for(tmp_path, monkeypatch, [shot])
    with client:
        item = client.get("/api/review").json()["items"][0]
        assert item["state"] == "queued" and not item["virtual"]
        assert (settings.observation_cache_root / "investigations.sqlite").exists()
        assert client.post(f"/api/review/{item['id']}/request").status_code == 403
        response = client.post(f"/api/review/{item['id']}/request", headers=headers(client))
        assert response.status_code == 200
        assert response.json()["state"] == "requested"
        assert not response.json()["virtual"]
        assert len(client.get("/api/review").json()["items"]) == 1
        assert not settings.t2_db.exists()
        again = client.post(f"/api/review/{item['id']}/request", headers=headers(client)).json()
        assert again["requested_utc"] == response.json()["requested_utc"]


def test_mirrored_miss_survives_ledger_ageout_and_request_is_not_reset(tmp_path, monkeypatch):
    shot = {"file_id": "inj_20260913_0001", "outcome": "missed_t1", "inject_utc": "2026-09-13T00:00:00Z"}
    misses = [shot]
    client, settings = client_for(tmp_path, monkeypatch, misses)
    with client:
        item = client.get("/api/review").json()["items"][0]
        misses.clear()
        retained = client.get("/api/review").json()["items"][0]
        assert retained == item and retained["state"] == "queued"
        requested = client.post(f"/api/review/{item['id']}/request", headers=headers(client)).json()
        misses.append(shot)
        assert client.get("/api/review").json()["items"][0] == requested


def test_nonworkspace_and_missing_source_do_not_create_database(tmp_path, monkeypatch):
    shot = {"file_id": "inj_20260913_0001", "outcome": "missed_t1", "inject_utc": "2026-09-13T00:00:00Z"}
    client, settings = client_for(tmp_path, monkeypatch, [shot], workspace=False)
    with client:
        result = client.get("/api/review").json()
        assert result["items"][0]["virtual"] and not result["writes_enabled"]
        assert client.post("/api/review", json={"title": "No"}, headers=headers(client)).status_code == 403
        assert not settings.observation_cache_root.exists()
    client, settings = client_for(tmp_path, monkeypatch)
    monkeypatch.setattr(review, "build_injection_overview", lambda s: {
        "misses": [], "source": str(s.t2_db), "status": "unavailable",
        "counts_complete": False, "trend_start_utc": "2026-09-08T00:00:00Z"})
    with client:
        assert client.get("/api/review").json()["items"] == []
        assert not settings.observation_cache_root.exists()


def test_saved_queue_pagination_is_explicit(tmp_path, monkeypatch):
    misses = [{"file_id": f"inj_20260913_{i:04d}", "outcome": "missed_t1", "inject_utc": "2026-09-13T00:00:00Z"}
              for i in range(3)]
    client, settings = client_for(tmp_path, monkeypatch, misses)
    with client:
        first = client.get("/api/review?limit=2").json()
        assert first["saved_total"] == 3 and first["saved_truncated"]
        assert first["next_offset"] == 2 and len(first["items"]) == 2
        last = client.get("/api/review?limit=2&offset=2").json()
        assert len(last["items"]) == 1 and not last["saved_truncated"]
        assert last["items"][0]["id"] not in {x["id"] for x in first["items"]}


def test_save_exact_pixels_and_origin_validation(tmp_path, monkeypatch):
    client, settings = client_for(tmp_path, monkeypatch)
    pid = "a" * 32
    directory = settings.observation_cache_root / "science" / pid
    directory.mkdir(parents=True)
    png = b"\x89PNG\r\n\x1a\nsaved-test-figure"
    (directory / "plot-0.png").write_bytes(png)
    body = {"title": "Phase structure", "note": "Inspect this interval", "selection": {"antennas": [9, 19]},
            "provenance": {"processing": "Sun fringe-stopped"}, "plot_url": f"/api/science/products/{pid}/plot-0.png"}
    with client:
        h = headers(client)
        assert client.post("/api/review", json=body, headers={**h, "Origin": "http://evil.invalid"}).status_code == 403
        created = client.post("/api/review", json=body, headers=h)
        assert created.status_code == 201
        item = created.json()
        assert item["state"] == "queued"
        (directory / "plot-0.png").unlink()
        assert client.get(item["saved_plot_url"]).content == png
        assert client.post("/api/review", json={**body, "plot_url": "https://evil.invalid/plot.png"}, headers=h).status_code == 400
        assert client.post("/api/review", json={**body, "provenance": {"text": "x" * 65536}, "plot_url": None}, headers=h).status_code == 413


def test_product_symlink_escape_refused(tmp_path, monkeypatch):
    client, settings = client_for(tmp_path, monkeypatch)
    pid = "b" * 32
    directory = settings.observation_cache_root / "science" / pid
    directory.mkdir(parents=True)
    target = tmp_path / "outside.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\nprivate")
    (directory / "plot-0.png").symlink_to(target)
    with client:
        response = client.post("/api/review", json={"title": "No", "plot_url": f"/api/science/products/{pid}/plot-0.png"}, headers=headers(client))
        assert response.status_code == 403
