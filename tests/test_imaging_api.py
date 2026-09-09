"""The Imaging figures router: manifest, whitelist, ETag/304, mp4, history."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from casm_monitor.web.figures import build_router

TS_A = 1788835440
TS_B = 1788835590
TS_OLD = 1788700000


@pytest.fixture
def seeded_tree(settings):
    root = settings.store_root / "figures" / "imaging"
    (root / "frames").mkdir(parents=True)
    (root / "latest@1x.png").write_bytes(b"\x89PNG\r\n\x1a\nlatest-1x")
    (root / "latest@2x.png").write_bytes(b"\x89PNG\r\n\x1a\nlatest-2x-bigger")
    (root / "strip24h@1x.png").write_bytes(b"\x89PNG\r\n\x1a\nstrip-1x")
    (root / "allsky24h.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
    for ts in (TS_OLD, TS_A, TS_B):
        (root / "frames" / f"{ts}@1x.png").write_bytes(f"\x89PNG frame {ts}".encode())
    manifest = {
        "rendered_utc": "2026-09-09T02:44:00Z",
        "cal_file": "cal_sep03peak_core17.h5",
        "antennas": [9, 10, 15],
        "latest": {"ts": "2026-09-09T02:41:00Z", "file_1x": "latest@1x.png",
                   "file_2x": "latest@2x.png"},
        "strip": {"t0": "2026-09-08T02:44:00Z", "t1": "2026-09-09T02:44:00Z", "n": 48,
                  "file_1x": "strip24h@1x.png", "file_2x": "strip24h@2x.png"},
        "movie": {"file": "allsky24h.mp4", "fps": 4},
        "sources": [{"name": "sun", "alt_deg": 12.3, "az_deg": 118.4, "up": True}],
        "psf_ceiling_snr": None,
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root, manifest


@pytest.fixture
def client(settings, seeded_tree):
    app = FastAPI()
    app.include_router(build_router(settings))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def bare_client(settings):
    app = FastAPI()
    app.include_router(build_router(settings))
    with TestClient(app) as c:
        yield c


def test_manifest(client, seeded_tree):
    _root, manifest = seeded_tree
    assert client.get("/api/figures/imaging/manifest").json() == manifest


def test_manifest_missing_is_404(bare_client):
    assert bare_client.get("/api/figures/imaging/manifest").status_code == 404


def test_png_etag_and_304(client):
    r = client.get("/api/figures/imaging/latest@1x.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "public, max-age=1800"
    etag = r.headers["etag"]
    again = client.get("/api/figures/imaging/latest@1x.png", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""


def test_mp4_content_type_and_ranges(client):
    r = client.get("/api/figures/imaging/allsky24h.mp4")
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["etag"]


def test_history_frame_is_served_under_the_same_route(client):
    r = client.get(f"/api/figures/imaging/frames/{TS_A}@1x.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


@pytest.mark.parametrize(
    "name",
    [
        "manifest.json",
        "latest@3x.png",
        "strip24h@1x.jpg",
        "frames/abc@1x.png",
        "frames/1788835440@2x.png",
        "frames/%2e%2e/%2e%2e/monitor.sqlite",
        "..%2Fmanifest.json",
    ],
)
def test_unknown_file_is_400_never_a_listing(client, name):
    assert client.get(f"/api/figures/imaging/{name}").status_code == 400


def test_whitelisted_but_unrendered_is_404(bare_client, settings):
    (settings.store_root / "figures" / "imaging").mkdir(parents=True)
    assert bare_client.get("/api/figures/imaging/latest@2x.png").status_code == 404


def test_history_window_filtering(client):
    body = client.get(
        "/api/imaging/history",
        params={"t0": "2026-09-08T00:00:00Z", "t1": "2026-09-09T23:59:59Z"},
    ).json()
    assert [f["ts_unix"] for f in body["frames"]] == [TS_A, TS_B]
    assert body["frames"][0]["file_1x"] == f"frames/{TS_A}@1x.png"
    assert body["frames"][0]["ts"].endswith("Z")


def test_history_narrow_window(client):
    body = client.get(
        "/api/imaging/history",
        params={"t0": "2026-09-07T00:00:00Z", "t1": "2026-09-07T12:00:00Z"},
    )
    # Empty window: an empty list, never a 404.
    assert body.status_code == 200
    assert body.json() == {"frames": []}


def test_history_unbounded_lists_everything(client):
    body = client.get("/api/imaging/history").json()
    assert [f["ts_unix"] for f in body["frames"]] == [TS_OLD, TS_A, TS_B]


def test_history_bad_timestamp_is_400(client):
    assert client.get("/api/imaging/history", params={"t0": "yesterday"}).status_code == 400


def test_history_with_no_frames_dir_is_empty(bare_client):
    assert bare_client.get("/api/imaging/history").json() == {"frames": []}
