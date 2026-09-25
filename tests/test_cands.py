"""Candidates tab (M5): the ``/api/cands`` router over a temp T2 sqlite.

Every test builds its own T2 database with ``casm_t2.db.connect`` (the
schema helper, same one t2d/t2-inject/casm_t3 all use) under ``tmp_path`` and
its own ``candidates_dir``; nothing here reads or writes the live
``/mnt/nvme5/casm_pipeline/db/t2.sqlite``. :func:`casm_monitor.web.cands` binds
``casm_t3.web.app``'s module-level ``DB_PATH``/``CANDIDATES_DIR`` to these tmp
paths per CALL (:func:`casm_monitor.web.cands._t3_bound`, see that module's
docstring), so two apps built against two different stores can be used in any
order — which :func:`test_two_routers_do_not_cross_contaminate` pins.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from casm_monitor.config import Settings
from casm_monitor.web import cands
from casm_monitor.web.app import create_app
from casm_monitor.web.cands import CSRF_COOKIE, CSRF_HEADER, canonical_since

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

def test_event_plot_inventory_opt_in_and_containment(seeded_db):
    event_dir = seeded_db.candidates_dir / NAME_DUMPED
    other = seeded_db.candidates_dir / NAME_HELD
    other.mkdir()
    (other/'private.png').write_bytes(b'not this event')
    (event_dir/'leak.png').symlink_to(other/'private.png')
    (event_dir/'bad name.png').write_bytes(b'invalid route name')
    with TestClient(create_app(seeded_db)) as client:
        assert 'plots' not in client.get('/api/cands/events').json()['events'][0]
        rows=client.get('/api/cands/events',params={'view':'all','include_plots':True}).json()['events']
        by_name={r['name']:r for r in rows}
        assert by_name[NAME_DUMPED]['plots']==[f'{NAME_DUMPED}.png']
        assert by_name[NAME_HELD]['plots']==['private.png']
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

        # The route only SERVES; nothing has rendered the funnel yet.
        plot = client.get("/api/cands/stats/plot.png")
        assert plot.status_code == 404
        assert "render_figures" in plot.json()["detail"]


def test_injections_route(cands_settings: Settings, monkeypatch) -> None:
    # The route's rolling-day count must use the fixture's date, not the wall
    # clock on whichever day pytest runs. Pagination itself is not time-limited.
    from casm_t3.web import statsplot
    now = datetime(2026, 9, 9, 18, tzinfo=timezone.utc)
    monkeypatch.setattr(statsplot, "utc_cut", lambda hours: (
        now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S"))
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
        assert res.json() == {"frbs": [], "limit": 200, "offset": 0}


def test_transits_route_has_snapshot_key(cands_settings: Settings) -> None:
    with client_for(cands_settings) as client:
        res = client.get("/api/cands/transits")
        assert res.status_code == 200
        assert "snapshot" in res.json()


# ---------------------------------------------------------------------------
# the 2026-09-09 review's findings
# ---------------------------------------------------------------------------


def test_two_routers_do_not_cross_contaminate(tmp_path: Path, seeded_db: Settings) -> None:
    """Finding 4: building a second router must not redirect the first.

    Before the per-call binding, ``build_router`` pointed ``casm_t3.web.app``'s
    process globals at its own settings once, so the app built LAST won for
    every app in the process.
    """
    other_db = tmp_path / "other" / "t2.sqlite"
    other_db.parent.mkdir()
    _connect(other_db).close()
    other = Settings(
        store_root=tmp_path / "other" / "store",
        t2_db=other_db,
        candidates_dir=tmp_path / "other" / "candidates",
        cadences={"obs": 30.0, "services": 30.0, "sky": 60.0, "disks": 300.0, "store": 300.0},
    )
    first = client_for(seeded_db)
    second = client_for(other)  # built AFTER the first is already in use
    try:
        assert second.get("/api/cands/events", params={"view": "all"}).json()["events"] == []
        names = {e["name"] for e in first.get("/api/cands/events", params={"view": "all"}).json()["events"]}
        assert names == {NAME_DUMPED, NAME_HELD}
        # And the module globals are back to whatever they were, not ours.
        from casm_t3.web import app as t3app

        assert str(t3app.DB_PATH) != str(seeded_db.t2_db)
        assert str(t3app.DB_PATH) != str(other.t2_db)
    finally:
        first.__exit__(None, None, None)
        second.__exit__(None, None, None)


def test_plot_refuses_a_symlink_escaping_the_event_directory(seeded_db: Settings) -> None:
    """Finding 5: containment is under candidates_dir/<name>, not the tree."""
    victim = seeded_db.candidates_dir / NAME_HELD
    victim.mkdir(parents=True)
    secret = victim / "secret.png"
    secret.write_bytes(b"\x89PNG\r\n other event's artefact")
    (seeded_db.candidates_dir / NAME_DUMPED / "peek.png").symlink_to(secret)
    with client_for(seeded_db) as client:
        # The same bytes ARE reachable through their own event.
        assert client.get(f"/api/cands/events/{NAME_HELD}/plot/secret.png").status_code == 200
        # ... but not through a symlink planted in another event's directory.
        res = client.get(f"/api/cands/events/{NAME_DUMPED}/plot/peek.png")
        assert res.status_code == 404


def test_since_canonicalises_the_space_form(seeded_db: Settings) -> None:
    """Finding 8: ``' ' < 'T'`` makes a space-form bound select the wrong rows."""
    conn = _connect(seeded_db.t2_db)
    try:
        _insert_cluster(conn, "260909cccccc", event_utc="2026-09-09T18:00:00.000")
        _insert_trigger(conn, "260909cccccc", "triggered", "ring ok")
    finally:
        conn.close()
    with client_for(seeded_db) as client:
        # Space-separated: without canonicalisation this compares as
        # "2026-09-09 15:00:00" < every "2026-09-09T..." row and returns both.
        res = client.get("/api/cands/events", params={"since": "2026-09-09 15:00:00"})
        assert res.status_code == 200
        assert {e["name"] for e in res.json()["events"]} == {"260909cccccc"}
        # The same instant in the T form, with a Z, and as an offset.
        for value in ("2026-09-09T15:00:00", "2026-09-09T15:00:00Z", "2026-09-09T08:00:00-07:00"):
            body = client.get("/api/cands/events", params={"since": value}).json()
            assert {e["name"] for e in body["events"]} == {"260909cccccc"}, value


def test_since_rejects_a_non_timestamp(seeded_db: Settings) -> None:
    with client_for(seeded_db) as client:
        res = client.get("/api/cands/events", params={"since": "yesterday"})
        assert res.status_code == 400
        assert "ISO-8601" in res.json()["detail"]


def test_canonical_since_is_the_db_t_form() -> None:
    assert canonical_since("2026-09-09 15:00:00") == "2026-09-09T15:00:00"
    assert canonical_since("2026-09-09T15:00:00.123456Z") == "2026-09-09T15:00:00"
    assert canonical_since("2026-09-09T08:00:00-07:00") == "2026-09-09T15:00:00"


def test_label_write_retries_a_locked_database_then_gives_up(
    seeded_db: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 6: SQLITE_BUSY is a 503 with a clear detail, never a 500."""
    monkeypatch.setattr(cands, "LABEL_BUSY_TIMEOUT_MS", 50)
    monkeypatch.setattr(cands, "LABEL_BACKOFF_S", 0.001)
    blocker = sqlite3.connect(seeded_db.t2_db, timeout=0.1)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        with client_for(seeded_db) as client:
            client.get("/api/cands/events")
            res = client.post(
                f"/api/cands/events/{NAME_DUMPED}/label",
                json={"label": "rfi", "note": ""},
                headers=csrf_headers(client),
            )
        assert res.status_code == 503
        assert "locked" in res.json()["detail"]
    finally:
        blocker.rollback()
        blocker.close()
    # The row really was not written.
    conn = sqlite3.connect(seeded_db.t2_db)
    try:
        assert conn.execute("SELECT count(*) FROM labels").fetchone()[0] == 0
    finally:
        conn.close()


def test_label_write_succeeds_once_the_lock_clears(
    seeded_db: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient SQLITE_BUSY is retried, not surfaced."""
    monkeypatch.setattr(cands, "LABEL_BACKOFF_S", 0.001)
    from casm_t3.web import app as t3app

    real_label = t3app.label
    attempts: list[int] = []

    def flaky(**kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise sqlite3.OperationalError("database is locked")
        return real_label(**kwargs)

    monkeypatch.setattr(t3app, "label", flaky)
    with client_for(seeded_db) as client:
        client.get("/api/cands/events")
        res = client.post(
            f"/api/cands/events/{NAME_DUMPED}/label",
            json={"label": "rfi", "note": "third time lucky"},
            headers=csrf_headers(client),
        )
    assert res.status_code == 200
    assert len(attempts) == 3
    assert res.json()["labels"][0]["notes"] == "third time lucky"


def test_events_auxiliary_queries_are_restricted_to_the_selected_names(
    seeded_db: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 7: triggers/labels are queried for the returned rows only."""
    from casm_t3.web import app as t3app

    conn = _connect(seeded_db.t2_db)
    try:
        for k in range(5):
            name = f"2609090000{k:02d}"[:12]
            _insert_cluster(conn, name, event_utc=f"2026-09-09T1{k}:00:00.000")
            _insert_trigger(conn, name, "triggered", "ring ok")
    finally:
        conn.close()

    seen: list[tuple[str, tuple]] = []
    real_q = t3app.q

    def spy(sql: str, args: tuple = ()):
        seen.append((sql, args))
        return real_q(sql, args)

    monkeypatch.setattr(t3app, "q", spy)
    with client_for(seeded_db) as client:
        body = client.get("/api/cands/events", params={"limit": 2}).json()
    assert len(body["events"]) == 2
    selected = {e["name"] for e in body["events"]}
    trigger_queries = [
        (sql, args) for sql, args in seen
        if sql.startswith("SELECT candname, action, detail FROM triggers")
    ]
    assert trigger_queries, "the outcome lookup must still run"
    for sql, args in trigger_queries:
        assert "candname IN" in sql
        assert set(args) == selected
    label_queries = [
        (sql, args) for sql, args in seen if sql.startswith("SELECT name, label FROM labels")
    ]
    for sql, args in label_queries:
        assert "name IN" in sql
        assert set(args) == selected


def test_frbs_and_injections_paginate(seeded_db: Settings) -> None:
    conn = _connect(seeded_db.t2_db)
    try:
        for k in range(5):
            conn.execute(
                "INSERT INTO frbs (name, event_utc, snr, dm, width, beam, notes,"
                " created_utc) VALUES (?, ?, 30.0, 120.0, 3, 42, '', ?)",
                (f"26090900000{k}", f"2026-09-09T1{k}:00:00.000", f"2026-09-09T1{k}:00:00.000"),
            )
            conn.execute(
                "INSERT INTO injections (inject_utc, stream, beam, dm, amp, sigma_ms,"
                " est_snr, file_id, gate_t1, gate_t2, created_utc) VALUES"
                " (?, 0, 10, 50.0, 1.0, 2.0, 20.0, ?, 1, 1, ?)",
                (f"2026-09-09T1{k}:00:00.000", f"f{k}", f"2026-09-09T1{k}:00:00.000"),
            )
        conn.commit()
    finally:
        conn.close()
    with client_for(seeded_db) as client:
        page = client.get("/api/cands/frbs", params={"limit": 2}).json()
        assert len(page["frbs"]) == 2 and page["limit"] == 2 and page["offset"] == 0
        second = client.get("/api/cands/frbs", params={"limit": 2, "offset": 2}).json()
        assert {f["name"] for f in second["frbs"]}.isdisjoint({f["name"] for f in page["frbs"]})
        assert client.get("/api/cands/frbs", params={"limit": 9001}).status_code == 422

        inj = client.get("/api/cands/injections", params={"limit": 2, "offset": 1}).json()
        assert len(inj["injections"]) == 2 and inj["offset"] == 1
        assert client.get("/api/cands/injections", params={"limit": 5000}).status_code == 422


def test_funnel_png_is_rendered_by_the_job_and_then_served(
    cands_settings: Settings, store
) -> None:
    """Finding 1: the GET route serves a file the render job wrote."""
    # Inside the chart's own window: statsplot bins against "now", so a row
    # stamped in the future (or older than the widest preset) is not a chart.
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    conn = _connect(cands_settings.t2_db)
    try:
        # Several rows spanning an hour: statsplot bins the gulps and a single
        # row gives it a zero-width span (a t3 quirk, not ours).
        for k in range(6):
            stamp = (now - timedelta(minutes=60 - 10 * k)).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "")
            conn.execute(
                "INSERT INTO gulp_stats (obs_utc_start, gulp, gulp_utc, n_jobs, n_cands,"
                " n_clusters, n_stored, n_would, clustering_ms, created_utc)"
                " VALUES ('2026-09-09-00:00:00', ?, ?, 8, 1000, 12, 3, 1, 5.5, ?)",
                (k, stamp, stamp),
            )
        conn.commit()
    finally:
        conn.close()

    from casm_monitor.jobs import render_figures as rf
    from casm_monitor.store import Store

    job_store = Store(cands_settings.db_path, store_root=cands_settings.store_root)
    try:
        result = rf._render_cands(job_store, cands_settings)
    finally:
        job_store.close()
    assert result["failed"] == {}
    root = cands_settings.store_root / "figures" / "cands"
    assert (root / "funnel@1x.png").is_file()
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["files"]["24"] == "funnel@1x.png"
    for hours in manifest["hours"]:
        assert (root / manifest["files"][str(hours)]).is_file()

    with client_for(cands_settings) as client:
        res = client.get("/api/cands/stats/plot.png")
        assert res.status_code == 200
        assert res.headers["content-type"] == "image/png"
        etag = res.headers["etag"]
        assert client.get(
            "/api/cands/stats/plot.png", headers={"if-none-match": etag}
        ).status_code == 304
        # Another preset window is its own file, not the 24 h one.
        wide = client.get("/api/cands/stats/plot.png", params={"hours": 168})
        assert wide.status_code == 200
        assert wide.headers["etag"] != etag


def test_funnel_route_never_writes(cands_settings: Settings) -> None:
    """The GET route must not create anything under the store root."""
    with client_for(cands_settings) as client:
        assert client.get("/api/cands/stats/plot.png").status_code == 404
    assert not (cands_settings.store_root / "figures" / "cands" / "funnel@1x.png").exists()
