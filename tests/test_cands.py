"""Candidates tab (M5): the ``/api/cands`` router over a temp T2 sqlite.

Every test builds its own T2 database with ``casm_t2.db.connect`` (the
schema helper, same one t2d/t2-inject/casm_t3 all use) under ``tmp_path`` and
its own ``candidates_dir``; nothing here reads or writes the live
``/mnt/nvme5/casm_pipeline/db/t2.sqlite``. :func:`casm_monitor.web.cands.build_router`
points ``casm_t3.web.app``'s module-level ``DB_PATH``/``CANDIDATES_DIR`` at
these tmp paths (see that module's docstring), so calling ``create_app``
twice with different settings in the same test session is safe as long as
each app is used before the next is built — exactly the pattern
``tests/test_cal.py`` already relies on for its own CSRF client.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from casm_monitor.config import Settings
from casm_monitor.web.app import create_app
from casm_monitor.web.cands import CSRF_COOKIE, CSRF_HEADER

NAME_DUMPED = "260909aabbcc"
NAME_HELD = "260909ddeeff"


def _connect(db_path: Path) -> sqlite3.Connection:
    from casm_t2 import db as t2db

    return t2db.connect(db_path)


def _insert_cluster(
    conn: sqlite3.Connection,
    name: str,
    *,
    tier: str = "A",
    tags: str = "",
    snr: float = 25.0,
    dm: float = 50.0,
    beam: int = 100,
    event_utc: str = "2026-09-09T12:00:00.000",
) -> int:
    cur = conn.execute(
        "INSERT INTO clusters (obs_utc_start, gulp, event_utc, samp, snr, dm, dm_idx,"
        " width, beam, n_members, n_beams, beam_lo, beam_hi, dm_lo, dm_hi, samp_lo,"
        " samp_hi, tier, tags, name, created_utc, alt_deg, az_deg)"
        " VALUES ('2026-09-09-00:00:00', 1, ?, 1000, ?, ?, 10, 3, ?, 5, 2, ?, ?, ?, ?,"
        " 990, 1010, ?, ?, ?, ?, 45.0, 90.0)",
        (
            event_utc,
            snr,
            dm,
            beam,
            beam,
            beam,
            dm,
            dm,
            tier,
            tags,
            name,
            event_utc,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_trigger(conn: sqlite3.Connection, candname: str, action: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO triggers (candname, stream, kind, action, detail, created_utc)"
        " VALUES (?, 0, 'intensity', ?, ?, '2026-09-09T12:00:01.000')",
        (candname, action, detail),
    )
    conn.commit()


@pytest.fixture
def cands_settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "t2.sqlite"
    _connect(db_path).close()
    return Settings(
        store_root=tmp_path / "store",
        t2_db=db_path,
        candidates_dir=tmp_path / "candidates",
        cadences={"obs": 30.0, "services": 30.0, "sky": 60.0, "disks": 300.0, "store": 300.0},
    )


@pytest.fixture
def seeded_db(cands_settings: Settings) -> Settings:
    conn = _connect(cands_settings.t2_db)
    try:
        _insert_cluster(conn, NAME_DUMPED, tier="A", snr=30.0, dm=120.0, beam=42)
        _insert_trigger(conn, NAME_DUMPED, "triggered", "ring ok")
        _insert_cluster(conn, NAME_HELD, tier="C", tags="rfi_wide", snr=13.0, dm=5.0, beam=7)
    finally:
        conn.close()
    art_dir = cands_settings.candidates_dir / NAME_DUMPED
    art_dir.mkdir(parents=True)
    (art_dir / f"{NAME_DUMPED}.png").write_bytes(b"\x89PNG\r\n fake")
    (art_dir / f"{NAME_DUMPED}.json").write_text('{"data_available": true, "context": {"members": [1, 2]}}')
    return cands_settings


def client_for(settings: Settings) -> TestClient:
    c = TestClient(create_app(settings))
    c.__enter__()
    return c


def csrf_headers(client: TestClient) -> dict[str, str]:
    return {CSRF_HEADER: client.cookies[CSRF_COOKIE]}


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def test_events_default_view_is_dump_attempts_only(seeded_db: Settings) -> None:
    with client_for(seeded_db) as client:
        res = client.get("/api/cands/events")
        assert res.status_code == 200
        names = {e["name"] for e in res.json()["events"]}
        # NAME_HELD never reached a trigger row and the default view is
        # "candidates" (dump attempts only), same default as t3-web's index().
        assert names == {NAME_DUMPED}
        assert CSRF_COOKIE in client.cookies


def test_events_view_all_includes_held_events_with_a_reason(seeded_db: Settings) -> None:
    with client_for(seeded_db) as client:
        res = client.get("/api/cands/events", params={"view": "all"})
        rows = {e["name"]: e for e in res.json()["events"]}
        assert set(rows) == {NAME_DUMPED, NAME_HELD}
        assert rows[NAME_HELD]["outcome"] == "RFI: too many beams"
        assert rows[NAME_DUMPED]["outcome"] == "dumped"
        assert rows[NAME_DUMPED]["snr"] == 30.0
        assert rows[NAME_DUMPED]["beam"] == 42


def test_events_tier_filter(seeded_db: Settings) -> None:
    with client_for(seeded_db) as client:
        res = client.get("/api/cands/events", params={"view": "all", "tier": "C"})
        names = {e["name"] for e in res.json()["events"]}
        assert names == {NAME_HELD}


# ---------------------------------------------------------------------------
# event detail + plots
# ---------------------------------------------------------------------------


def test_event_detail_shape_and_data_status(seeded_db: Settings) -> None:
    with client_for(seeded_db) as client:
        res = client.get(f"/api/cands/events/{NAME_DUMPED}")
        assert res.status_code == 200
        body = res.json()
        assert body["event"]["name"] == NAME_DUMPED
        assert body["plots"] == [f"{NAME_DUMPED}.png"]
        assert body["data_status"] == "raw dump on disk"
        assert body["triggers"][0]["action"] == "triggered"
        assert "members" not in body["meta"].get("context", {})
        assert body["label_choices"] == ["frb", "pulsar", "rfi", "unsure"]


def test_event_detail_unknown_event_is_404(seeded_db: Settings) -> None:
    with client_for(seeded_db) as client:
        res = client.get("/api/cands/events/260909zzzzzz")
        assert res.status_code == 404


def test_event_detail_invalid_name_is_400(seeded_db: Settings) -> None:
    with client_for(seeded_db) as client:
        res = client.get("/api/cands/events/not-a-name")
        assert res.status_code == 400


def test_plot_serves_png_with_etag(seeded_db: Settings) -> None:
    with client_for(seeded_db) as client:
        res = client.get(f"/api/cands/events/{NAME_DUMPED}/plot/{NAME_DUMPED}.png")
        assert res.status_code == 200
        assert res.headers["content-type"] == "image/png"
        etag = res.headers["etag"]
        res2 = client.get(
            f"/api/cands/events/{NAME_DUMPED}/plot/{NAME_DUMPED}.png",
            headers={"if-none-match": etag},
        )
        assert res2.status_code == 304


def test_plot_rejects_unlisted_extension(seeded_db: Settings) -> None:
    with client_for(seeded_db) as client:
        res = client.get(f"/api/cands/events/{NAME_DUMPED}/plot/evil.sh")
        assert res.status_code == 400


def test_plot_rejects_path_traversal(seeded_db: Settings) -> None:
    with client_for(seeded_db) as client:
        # Encoded so the client actually sends the traversal in the path
        # segment rather than requesting a different route.
        res = client.get(f"/api/cands/events/{NAME_DUMPED}/plot/..%2F..%2Fetc%2Fpasswd.png")
        assert res.status_code in (400, 404)


def test_plot_missing_file_is_404(seeded_db: Settings) -> None:
    with client_for(seeded_db) as client:
        res = client.get(f"/api/cands/events/{NAME_DUMPED}/plot/nope.png")
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# labeling + CSRF
# ---------------------------------------------------------------------------


def test_label_post_without_csrf_cookie_is_refused(seeded_db: Settings) -> None:
    with client_for(seeded_db) as client:
        res = client.post(f"/api/cands/events/{NAME_DUMPED}/label", json={"label": "frb", "note": "x"})
        assert res.status_code == 403


def test_label_post_with_mismatched_header_is_refused(seeded_db: Settings) -> None:
    with client_for(seeded_db) as client:
        client.get("/api/cands/events")  # mints the cookie
        res = client.post(
            f"/api/cands/events/{NAME_DUMPED}/label",
            json={"label": "frb", "note": "x"},
            headers={CSRF_HEADER: "wrong-token"},
        )
        assert res.status_code == 403


def test_label_post_bad_label_value_is_400(seeded_db: Settings) -> None:
    with client_for(seeded_db) as client:
        client.get("/api/cands/events")
        res = client.post(
            f"/api/cands/events/{NAME_DUMPED}/label",
            json={"label": "nonsense", "note": ""},
            headers=csrf_headers(client),
        )
        assert res.status_code == 400


def test_label_post_writes_row_and_promotes_frb(seeded_db: Settings) -> None:
    with client_for(seeded_db) as client:
        client.get("/api/cands/events")
        res = client.post(
            f"/api/cands/events/{NAME_DUMPED}/label",
            json={"label": "frb", "note": "looks real"},
            headers=csrf_headers(client),
        )
        assert res.status_code == 200
        body = res.json()
        assert body["label"] == "frb"
        assert body["labels"][0]["who"] == "monitor"
        assert body["labels"][0]["notes"] == "looks real"

    # Written straight to the sqlite file, independent of the app that wrote
    # it (this IS the same file t3-web reads).
    conn = sqlite3.connect(seeded_db.t2_db)
    try:
        labels = conn.execute("SELECT label, who FROM labels WHERE name = ?", (NAME_DUMPED,)).fetchall()
        assert labels == [("frb", "monitor")]
        frbs = conn.execute("SELECT name FROM frbs WHERE name = ?", (NAME_DUMPED,)).fetchall()
        assert frbs == [(NAME_DUMPED,)]
    finally:
        conn.close()


def test_label_post_non_frb_label_does_not_promote(seeded_db: Settings) -> None:
    with client_for(seeded_db) as client:
        client.get("/api/cands/events")
        client.post(
            f"/api/cands/events/{NAME_DUMPED}/label",
            json={"label": "rfi", "note": ""},
            headers=csrf_headers(client),
        )
    conn = sqlite3.connect(seeded_db.t2_db)
    try:
        frbs = conn.execute("SELECT name FROM frbs WHERE name = ?", (NAME_DUMPED,)).fetchall()
        assert frbs == []
    finally:
        conn.close()


def test_label_post_unknown_event_is_404(seeded_db: Settings) -> None:
    with client_for(seeded_db) as client:
        client.get("/api/cands/events")
        res = client.post(
            "/api/cands/events/260909zzzzzz/label",
            json={"label": "frb", "note": ""},
            headers=csrf_headers(client),
        )
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# stats / injections / frbs / transits
# ---------------------------------------------------------------------------


def test_stats_route_and_funnel_plot(cands_settings: Settings) -> None:
    conn = _connect(cands_settings.t2_db)
    try:
        conn.execute(
            "INSERT INTO gulp_stats (obs_utc_start, gulp, gulp_utc, n_jobs, n_cands,"
            " n_clusters, n_stored, n_would, clustering_ms, created_utc)"
            " VALUES ('2026-09-09-00:00:00', 1, '2026-09-09T12:00:00.000', 8, 1000,"
            " 12, 3, 1, 5.5, '2026-09-09T12:00:00.000')"
        )
        conn.commit()
    finally:
        conn.close()
    with client_for(cands_settings) as client:
        res = client.get("/api/cands/stats", params={"hours": 24})
        assert res.status_code == 200
        body = res.json()
        assert body["hours"] == 24
        assert len(body["rows"]) == 1
        assert body["rows"][0]["n_cands"] == 1000

        plot = client.get("/api/cands/stats/plot.png")
        assert plot.status_code == 200
        assert plot.headers["content-type"] == "image/png"


def test_injections_route(cands_settings: Settings) -> None:
    conn = _connect(cands_settings.t2_db)
    try:
        conn.execute(
            "INSERT INTO injections (inject_utc, stream, beam, dm, amp, sigma_ms,"
            " est_snr, file_id, gate_t1, gate_t2, created_utc) VALUES"
            " ('2026-09-09T12:00:00.000', 0, 10, 50.0, 1.0, 2.0, 20.0, 'abc', 1, 1,"
            " '2026-09-09T12:00:00.000')"
        )
        conn.commit()
    finally:
        conn.close()
    with client_for(cands_settings) as client:
        res = client.get("/api/cands/injections")
        assert res.status_code == 200
        body = res.json()
        assert len(body["injections"]) == 1
        assert body["day"]["n"] == 1


def test_frbs_route_empty(cands_settings: Settings) -> None:
    with client_for(cands_settings) as client:
        res = client.get("/api/cands/frbs")
        assert res.status_code == 200
        assert res.json() == {"frbs": []}


def test_transits_route_has_snapshot_key(cands_settings: Settings) -> None:
    with client_for(cands_settings) as client:
        res = client.get("/api/cands/transits")
        assert res.status_code == 200
        assert "snapshot" in res.json()
