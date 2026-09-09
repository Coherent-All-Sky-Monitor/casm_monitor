"""The SNAPs figures router: manifest/list/PNG serving, ETag/304 and path
validation, mirroring the Vis figures router's own test suite."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from casm_monitor.web.figures import build_router


@pytest.fixture
def seeded_tree(settings):
    root = settings.store_root / "figures" / "snaps" / "beamforming"
    root.mkdir(parents=True)
    (root / "spectra_correlator@1x.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-1x")
    (root / "spectra_correlator@2x.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-2x-bigger")
    manifest = {
        "rendered_utc": "2026-09-09T02:12:00Z",
        "set": "beamforming",
        "t0": 1788839320.0,
        "t1": 1788925720.0,
        "n_frames": 144,
        "board_read_ts": 1788925000.0,
        "t1_kafka": 1788925720.0,
        "files": {
            "spectra_correlator": {"1x": "spectra_correlator@1x.png", "2x": "spectra_correlator@2x.png"}
        },
        "kinds": ["spectra_correlator"],
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
    body = client.get("/api/figures/snaps/manifest?set=beamforming").json()
    assert body == manifest


def test_manifest_missing_is_404(client):
    assert client.get("/api/figures/snaps/manifest?set=all12").status_code == 404


def test_manifest_bad_set_is_400(client):
    assert client.get("/api/figures/snaps/manifest?set=nope").status_code == 400


def test_list_combos(client):
    body = client.get("/api/figures/snaps/list").json()
    assert {
        "set": "beamforming",
        "rendered_utc": "2026-09-09T02:12:00Z",
        "board_read_ts": 1788925000.0,
        "kinds": ["spectra_correlator"],
    } in body["combos"]
    assert "spectra_correlator" in body["kinds"]
    assert set(body["sets"]) == {"beamforming", "all12"}


def test_png_1x_and_2x_and_etag_304(client):
    r1 = client.get("/api/figures/snaps/beamforming/spectra_correlator@1x.png")
    assert r1.status_code == 200
    assert r1.headers["content-type"] == "image/png"
    assert r1.headers["cache-control"] == "public, max-age=1800"
    assert "last-modified" in r1.headers
    etag = r1.headers["etag"]
    assert len(etag) == 16

    r2 = client.get("/api/figures/snaps/beamforming/spectra_correlator@2x.png")
    assert r2.status_code == 200
    assert len(r2.content) > len(r1.content)

    r304 = client.get(
        "/api/figures/snaps/beamforming/spectra_correlator@1x.png",
        headers={"If-None-Match": etag},
    )
    assert r304.status_code == 304
    assert r304.content == b""


def test_png_unrendered_kind_is_404(client):
    r = client.get("/api/figures/snaps/beamforming/trend@1x.png")
    assert r.status_code == 404


def test_png_bad_kind_or_suffix_is_400(client):
    assert client.get("/api/figures/snaps/beamforming/not_a_kind@1x.png").status_code == 400
    assert client.get("/api/figures/snaps/beamforming/spectra_correlator@3x.png").status_code == 400


def test_path_traversal_is_refused(client):
    r = client.get("/api/figures/snaps/manifest?set=..%2F..%2Fetc")
    assert r.status_code == 400


def test_vis_router_still_mounted(client):
    # The combined router still serves the Vis half unchanged.
    assert client.get("/api/figures/vis/manifest?set=nope&ref=raw").status_code == 400
