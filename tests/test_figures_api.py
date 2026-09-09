"""The figures router: manifest/list/PNG serving, ETag/304 and path validation."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from casm_monitor.web.figures import build_router


@pytest.fixture
def seeded_tree(settings):
    root = settings.store_root / "figures" / "vis" / "wired" / "raw"
    root.mkdir(parents=True)
    (root / "matrix_amp@1x.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-1x")
    (root / "matrix_amp@2x.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-2x-bigger")
    manifest = {
        "rendered_utc": "2026-09-09T02:12:00Z",
        "set": "wired",
        "ref": "raw",
        "t0": 1788839320.0,
        "t1": 1788925720.0,
        "n_integrations": 144,
        "stream": "vis_avg8",
        "obs": "2026-09-04-16:43:47",
        "files": {"matrix_amp": {"1x": "matrix_amp@1x.png", "2x": "matrix_amp@2x.png"}},
        "kinds": ["matrix_amp"],
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root, manifest


@pytest.fixture
def client(settings, seeded_tree):
    app = FastAPI()
    app.include_router(build_router(settings))
    with TestClient(app) as c:
        yield c


def test_manifest(client, seeded_tree):
    _root, manifest = seeded_tree
    body = client.get("/api/figures/vis/manifest?set=wired&ref=raw").json()
    assert body == manifest


def test_manifest_missing_is_404(client):
    assert client.get("/api/figures/vis/manifest?set=live&ref=sun").status_code == 404


def test_manifest_bad_set_or_ref_is_400(client):
    assert client.get("/api/figures/vis/manifest?set=nope&ref=raw").status_code == 400
    assert client.get("/api/figures/vis/manifest?set=wired&ref=nope").status_code == 400


def test_list_combos(client):
    body = client.get("/api/figures/vis/list").json()
    assert {"set": "wired", "ref": "raw", "rendered_utc": "2026-09-09T02:12:00Z", "kinds": ["matrix_amp"]} in body["combos"]
    assert "matrix_amp" in body["kinds"]


def test_png_1x_and_2x_and_etag_304(client):
    r1 = client.get("/api/figures/vis/wired/raw/matrix_amp@1x.png")
    assert r1.status_code == 200
    assert r1.headers["content-type"] == "image/png"
    assert r1.headers["cache-control"] == "public, max-age=1800"
    assert "last-modified" in r1.headers
    etag = r1.headers["etag"]
    assert len(etag) == 16

    r2 = client.get("/api/figures/vis/wired/raw/matrix_amp@2x.png")
    assert r2.status_code == 200
    assert len(r2.content) > len(r1.content)

    r304 = client.get(
        "/api/figures/vis/wired/raw/matrix_amp@1x.png", headers={"If-None-Match": etag}
    )
    assert r304.status_code == 304
    assert r304.content == b""


def test_png_unrendered_kind_is_404(client):
    r = client.get("/api/figures/vis/wired/raw/autos@1x.png")
    assert r.status_code == 404


def test_png_bad_kind_or_suffix_is_400(client):
    assert client.get("/api/figures/vis/wired/raw/not_a_kind@1x.png").status_code == 400


def test_path_traversal_is_refused(client):
    # FastAPI's own routing normalises "..", so this exercises the whitelist
    # a second way: a crafted set/ref/kind is validated before any file I/O.
    r = client.get("/api/figures/vis/manifest?set=..%2F..%2Fetc&ref=raw")
    assert r.status_code == 400
