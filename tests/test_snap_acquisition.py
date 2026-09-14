"""The preview can bridge one guarded diagnostic request, never another job."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from casm_monitor.config import Settings
from casm_monitor.store import Store
from casm_monitor.web import snap_acquisition as module


def setup(tmp_path, monkeypatch, *, workspace=True, result=(200, {"job_id": 12})):
    monkeypatch.setenv("CASM_MONITOR_WORKSPACE", "1" if workspace else "0")
    settings = Settings(store_root=tmp_path / "store", observation_cache_root=tmp_path / "preview",
                        snap_read_interval_s=7200)
    writer = Store(settings.db_path, store_root=settings.store_root)
    reader = Store(settings.db_path, store_root=settings.store_root, read_only=True)
    monkeypatch.setattr(module, "all_boards", lambda s: [SimpleNamespace(ip="192.168.120.52")])
    from casm_monitor.web import snapread
    monkeypatch.setattr(snapread, "all_boards", lambda s: [SimpleNamespace(ip="192.168.120.52")])
    calls = []

    def submit(ips):
        calls.append(ips)
        return result

    app = FastAPI()
    app.include_router(module.build_router(settings, reader, submit=submit))
    return TestClient(app), writer, reader, calls


def headers(client):
    token = client.get("/api/snap-workspace/acquisition").json()["csrf_token"]
    return {"Origin": "http://testserver", "X-CASM-Workspace": "1", "X-CASM-Snap-CSRF": token}


def test_status_reads_evidence_only_and_reports_missing_cadence(tmp_path, monkeypatch):
    client, writer, reader, calls = setup(tmp_path, monkeypatch)
    with client:
        data = client.get("/api/snap-workspace/acquisition").json()
        assert data["configured_interval_s"] == 7200 and data["due"]
        assert data["age_s"] is None and not data["cadence_verified"]
        assert calls == [] and writer.list_jobs() == []
    reader.close(); writer.close()


def test_only_confirmed_guarded_allowlisted_request_is_forwarded(tmp_path, monkeypatch):
    client, writer, reader, calls = setup(tmp_path, monkeypatch)
    with client:
        path = "/api/snap-workspace/acquire"
        assert client.post(path, json={"confirm": True}).status_code == 403
        h = headers(client)
        assert client.post(path, json={"confirm": False}, headers=h).status_code == 400
        assert client.post(path, json={"confirm": True, "ips": ["evil"]}, headers=h).status_code == 400
        assert client.post(path, json={"confirm": True, "kind": "deploy_upload"}, headers=h).status_code == 422
        assert client.post(path, json={"confirm": True}, headers={**h, "Origin": "http://evil.invalid"}).status_code == 403
        assert calls == []
        response = client.post(path, json={"confirm": True, "ips": ["192.168.120.52", "192.168.120.52"]}, headers=h)
        assert response.json() == {"job_id": 12}
        assert calls == [["192.168.120.52"]]
        assert writer.list_jobs() == []  # The bridge never writes a local job.
    reader.close(); writer.close()


def test_upstream_cooldown_is_preserved_and_nonworkspace_refused(tmp_path, monkeypatch):
    client, writer, reader, calls = setup(tmp_path, monkeypatch, result=(429, {"detail": "read pending", "retry_after_s": 10}))
    with client:
        response = client.post("/api/snap-workspace/acquire", json={"confirm": True}, headers=headers(client))
        assert response.status_code == 429 and response.json()["retry_after_s"] == 10
        assert calls == [None]
    reader.close(); writer.close()
    client, writer, reader, calls = setup(tmp_path, monkeypatch, workspace=False)
    with client:
        assert not client.get("/api/snap-workspace/acquisition").json()["manual_enabled"]
        assert client.post("/api/snap-workspace/acquire", json={"confirm": True}, headers=headers(client)).status_code == 403
        assert calls == []
    reader.close(); writer.close()


def test_transport_is_fixed_no_proxy_no_redirect_and_bounded(monkeypatch):
    captured = {}

    class Response:
        code = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, n):
            captured["max_read"] = n
            return b'{"job_id":8}'

    class Opener:
        def open(self, request, timeout):
            captured.update(url=request.full_url, method=request.method, body=request.data, timeout=timeout)
            return Response()

    def opener(*handlers):
        captured["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(module.urllib.request, "build_opener", opener)
    assert module.submit_existing(None) == (200, {"job_id": 8})
    assert captured["url"] == module.PRODUCTION_URL
    assert captured["method"] == "POST" and captured["body"] == b'{"ips": null}'
    assert captured["timeout"] == 5 and captured["max_read"] == 65537
    assert captured["handlers"][0].proxies == {}
    assert isinstance(captured["handlers"][1], module.NoRedirect)
