"""Isolated annotation tests; all candidate ledgers and products are temporary."""
import hashlib
import json
import sqlite3
import uuid
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from casm_monitor.config import Settings
from casm_monitor.web.candidate_annotations import build_router, annotation_root
from test_cands import _connect, _insert_cluster, _insert_trigger

NAME = "261006aabbcc"
PNG = b"\x89PNG\r\n\x1a\nexample-plot"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("CASM_MONITOR_WORKSPACE", "1")
    settings = Settings(store_root=tmp_path/"store", observation_cache_root=tmp_path/"preview",
                        candidate_annotations_root=tmp_path/"annotations", t2_db=tmp_path/"t2.sqlite",
                        candidates_dir=tmp_path/"candidates", candidate_archive_root=tmp_path/"archive")
    with _connect(settings.t2_db) as conn:
        _insert_cluster(conn, NAME)
        _insert_trigger(conn, NAME, "triggered")
    directory = settings.candidates_dir/NAME
    directory.mkdir(parents=True)
    (directory/f"{NAME}.png").write_bytes(PNG)
    (directory/f"{NAME}.json").write_text('{"plot_version":3}')
    archive = settings.candidate_archive_root/NAME
    archive.mkdir(parents=True)
    (archive/f"{NAME}.fil").write_bytes(b"test-only-filterbank")
    app = FastAPI()
    app.include_router(build_router(settings))
    with TestClient(app) as client:
        yield client, settings


def headers(client):
    return {"Origin":"http://testserver", "X-CASM-Workspace":"1",
            "X-CASM-Annotations-CSRF":client.get("/api/candidate-annotations").json()["csrf_token"]}


def reviewer(client, name="Vishnu"):
    res = client.post("/api/candidate-annotations/reviewers", json={"name":name}, headers=headers(client))
    assert res.status_code == 200, res.text
    return res.json()


def payload(who, **kw):
    return {"reviewer_id":who["id"], "request_id":uuid.uuid4().hex, "expected_revision":0,
            "label":"frb", "plot":f"{NAME}.png", "plot_sha256":hashlib.sha256(PNG).hexdigest(), **kw}


def save(client, body, status=200):
    res = client.post(f"/api/candidate-annotations/events/{NAME}", json=body, headers=headers(client))
    assert res.status_code == status, res.text
    return res.json()


def test_read_does_not_create_storage_and_production_never_written(setup):
    client, settings = setup
    before = settings.t2_db.read_bytes()
    meta = client.get("/api/candidate-annotations").json()
    assert meta["votes"] == [] and meta["writes_enabled"]
    assert not annotation_root(settings).exists()
    who = reviewer(client)
    result = save(client, payload(who))
    assert result["votes"][0]["label"] == "frb"
    assert settings.t2_db.read_bytes() == before
    with sqlite3.connect(settings.t2_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM frbs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 0
    assert len(list((annotation_root(settings)/"backups").glob("*.sqlite3"))) == 1


def test_independent_reviewers_normalization_and_append_only_undo(setup):
    client, settings = setup
    a, b = reviewer(client), reviewer(client, "Liam")
    assert reviewer(client, "  vishnu  ")["id"] == a["id"]
    one = save(client, payload(a))
    save(client, payload(b, label="pulsar"))
    two = save(client, payload(a, label="rfi", expected_revision=one["revision"], morphology=["narrowband", "repeating"]))
    assert len(two["votes"]) == 2
    undo = save(client, payload(a, action="undo", expected_revision=two["revision"]))
    assert next(v for v in undo["votes"] if v["reviewer_id"]==a["id"])["label"] == "frb"
    clear = save(client, payload(a, action="undo", expected_revision=undo["revision"]))
    assert next(v for v in clear["votes"] if v["reviewer_id"]==a["id"])["quality"] == "unreviewed"
    save(client, payload(a, action="undo", expected_revision=clear["revision"]), 400)
    with sqlite3.connect(annotation_root(settings)/"annotations.sqlite3") as conn:
        assert conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1


def test_idempotency_conflicts_and_invalid_input(setup):
    client, _ = setup
    body = payload(reviewer(client))
    one = save(client, body)
    assert save(client, body)["revision"] == one["revision"]
    save(client, {**body, "label":"noise"}, 409)
    save(client, {**body, "request_id":uuid.uuid4().hex}, 409)
    save(client, {**body, "request_id":uuid.uuid4().hex, "expected_revision":one["revision"], "label":"made-up"}, 422)
    save(client, {**body, "request_id":uuid.uuid4().hex, "expected_revision":one["revision"], "note":"x"*2001}, 422)


def test_extended_rfi_taxonomy_preserves_existing_assessments(setup, monkeypatch):
    from casm_monitor.web import candidate_annotations as module
    client, settings = setup
    who = reviewer(client)
    monkeypatch.setattr(module, "TAXONOMY_VERSION", 1)
    first = save(client, payload(who, label="rfi", morphology=["narrowband", "repeating"]))
    with sqlite3.connect(annotation_root(settings)/"annotations.sqlite3") as conn:
        original = conn.execute("SELECT * FROM annotations WHERE id=?", (first["revision"],)).fetchone()
    monkeypatch.setattr(module, "TAXONOMY_VERSION", 2)
    second = save(client, payload(who, label="rfi", expected_revision=first["revision"], morphology=list(module.MORPHOLOGY)))
    assert second["votes"][0]["taxonomy_version"] == 2
    assert set(second["votes"][0]["morphology"]) == set(module.MORPHOLOGY)
    save(client, payload(who, label="rfi", expected_revision=second["revision"], morphology=["invented"]), 422)
    with sqlite3.connect(annotation_root(settings)/"annotations.sqlite3") as conn:
        assert conn.execute("SELECT * FROM annotations WHERE id=?", (first["revision"],)).fetchone() == original
    rows = [json.loads(line) for line in client.get("/api/candidate-annotations/export").text.splitlines()]
    assert rows[0]["taxonomy_version"] == 2
    assert rows[0]["morphology"] == list(module.MORPHOLOGY)
    assert client.get("/api/candidate-annotations").json()["taxonomy_version"] == 2


def test_retained_products_survive_removal_and_preserve_exact_bytes(setup):
    client, settings = setup
    who = reviewer(client)
    one = save(client, payload(who))
    rows = [json.loads(line) for line in client.get("/api/candidate-annotations/export").text.splitlines()]
    evidence = next(r["body"] for r in rows if r["type"] == "evidence")
    assert evidence["filterbank_status"] == "retained"
    assert client.get(evidence["plot"]["url"]).content == PNG
    assert client.get(evidence["filterbank"]["url"]).content == b"test-only-filterbank"
    (settings.candidates_dir/NAME/f"{NAME}.png").unlink()
    with _connect(settings.t2_db) as conn:
        conn.execute("DELETE FROM clusters WHERE name=?", (NAME,))
    detail = client.get(f"/api/candidate-annotations/events/{NAME}").json()
    assert detail["plots"][0]["retained"]
    assert client.get(detail["plots"][0]["url"]).content == PNG
    two = save(client, payload(who, label="unsure", expected_revision=one["revision"]))
    assert two["votes"][0]["label"] == "unsure"


def test_changed_plot_is_not_silently_relabelled(setup):
    client, settings = setup
    who = reviewer(client)
    detail = client.get(f"/api/candidate-annotations/events/{NAME}").json()
    (settings.candidates_dir/NAME/f"{NAME}.png").write_bytes(PNG+b"new")
    assert client.get(detail["plots"][0]["url"]).status_code == 409
    save(client, payload(who), 409)
    assert client.get("/api/candidate-annotations?names="+NAME).json()["votes"] == []


def test_missing_plot_never_becomes_noise(setup):
    client, settings = setup
    who = reviewer(client)
    (settings.candidates_dir/NAME/f"{NAME}.png").unlink()
    save(client, payload(who, plot=None, plot_sha256=None, label="noise"), 400)
    saved = save(client, payload(who, plot=None, plot_sha256=None, label=None, quality="cannot_assess"))
    assert saved["votes"][0]["label"] is None
    assert saved["votes"][0]["quality"] == "cannot_assess"
    (settings.candidates_dir/NAME/f"{NAME}.png").write_bytes(PNG)
    save(client, payload(who, expected_revision=saved["revision"], label="noise"))


def test_origin_token_and_symlink_guards(setup, tmp_path):
    client, settings = setup
    for h in ({}, {**headers(client), "Origin":"https://evil.invalid"},
              {**headers(client), "X-CASM-Annotations-CSRF":"wrong"}):
        assert client.post("/api/candidate-annotations/reviewers", json={"name":"No"}, headers=h).status_code == 403
    who = reviewer(client)
    file = settings.candidates_dir/NAME/f"{NAME}.png"
    file.unlink()
    outside = tmp_path/"outside.png"
    outside.write_bytes(PNG)
    file.symlink_to(outside)
    save(client, payload(who), 403)
    save(client, payload(who, plot="../outside.png"), 400)


def test_root_protection_and_workspace_disabled(setup, monkeypatch):
    client, settings = setup
    for root in (settings.store_root, settings.store_root/"annotations", settings.candidates_dir, settings.store_root.parent):
        with pytest.raises(ValueError):
            annotation_root(replace(settings, candidate_annotations_root=root))
    monkeypatch.delenv("CASM_MONITOR_WORKSPACE")
    app = FastAPI()
    app.include_router(build_router(settings))
    with TestClient(app) as locked:
        assert not locked.get("/api/candidate-annotations").json()["writes_enabled"]
        assert locked.post("/api/candidate-annotations/reviewers", json={"name":"No"}, headers=headers(locked)).status_code == 403


def test_full_app_allows_only_annotations_and_requires_both_guards(setup, monkeypatch):
    from casm_monitor.store import Store
    from casm_monitor.web.app import create_app
    _, settings = setup
    with Store(settings.db_path, store_root=settings.store_root):
        pass
    with TestClient(create_app(settings, read_only=True)) as client:
        who = reviewer(client)
        save(client, payload(who))
        assert client.post(f"/api/cands/events/{NAME}/label", json={"label":"frb"}, headers=headers(client)).status_code == 403
        assert client.post("/api/jobs", json={}, headers=headers(client)).status_code == 403
        assert client.put(f"/api/candidate-annotations/events/{NAME}", json=payload(who), headers=headers(client)).status_code == 403
