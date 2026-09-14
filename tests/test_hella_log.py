"""Gulp ledger: parsing, the byte watermark and the first-run tail rule."""
import sqlite3
import time
from datetime import datetime, timezone

from casm_monitor import hella_log

PROCESSED = ("4 [2026-09-14 10:59:20.295] [info] processed 8.72415232 s in read 0.125628 "
             "flag 0.522513 dedisp 3.7786329 smooth 1.0319849 peak 0.17115594 output 0.000261 "
             "[5.6301756]")
CAP = "7 [2026-09-14 10:58:51.404] [warning] Only processed 25/64 beams - detected 10000 peaks"
CAPPED = ("7 [2026-09-14 10:58:51.500] [info] processed 8.72415232 s in read 0.1 flag 0.2 "
          "dedisp 3.3 smooth 1.0 peak 0.17 output 0.0003 [5.1]")
CHANSTAT = ("4 [2026-09-14 10:59:20.295] [info] chanstat blanked 472 specflag 0 usable 0.8464 "
            "clip_lo 3.656e-06 clip_hi 1.987e-06")
OTHER = "4 [2026-09-14 10:59:21.000] [info] sending data"


def test_parses_processed_cap_and_chanstat():
    rows = hella_log.parse_lines([PROCESSED, CHANSTAT, OTHER])
    assert len(rows) == 2 and rows[0] is rows[1]
    row = rows[0]
    assert row["stream"] == 4 and row["cap_hit"] == 0 and row["blanked"] == 472
    assert row["wall_s"] == 5.6301756 and row["dedisp_s"] == 3.7786329
    # Log clock is OVRO local: 10:59:20 PDT is 17:59:20 UTC.
    assert datetime.fromtimestamp(row["ts_unix"], timezone.utc) == datetime(
        2026, 9, 14, 17, 59, 20, 295000, tzinfo=timezone.utc)


def test_cap_warning_attaches_to_the_next_processed_line_of_that_stream():
    rows = hella_log.parse_lines([CAP, PROCESSED, CAPPED])
    by_stream = {row["stream"]: row for row in rows}
    assert by_stream[4]["cap_hit"] == 0 and by_stream[4]["beams_processed"] is None
    assert by_stream[7]["cap_hit"] == 1
    assert by_stream[7]["beams_processed"] == 25 and by_stream[7]["beams_total"] == 64
    assert by_stream[7]["peaks"] == 10000


def write_log(path, lines):
    path.write_text("".join(line + "\n" for line in lines))


def test_watermark_advances_without_duplicates(tmp_path):
    log = tmp_path / "hella.log"
    write_log(log, [PROCESSED, CHANSTAT])
    ledger = hella_log.HellaLogLedger(tmp_path / "hella_gulps.sqlite", log)
    first = ledger.tick()
    assert first["status"] == "ok" and first["offset"] == log.stat().st_size
    with log.open("a") as handle:
        handle.write(CAP + "\n" + CAPPED + "\n")
    second = ledger.tick()
    assert second["offset"] == log.stat().st_size and second["offset"] > first["offset"]
    connection = ledger.connect()
    rows = connection.execute("SELECT stream, cap_hit, blanked FROM gulps ORDER BY stream").fetchall()
    assert rows == [(4, 0, 472), (7, 1, None)]
    # A third tick over no new bytes must not duplicate or lose anything.
    ledger.tick()
    assert connection.execute("SELECT count(*) FROM gulps").fetchone()[0] == 2
    ledger.close()


def test_inode_change_restarts_from_the_tail_rule(tmp_path, monkeypatch):
    log = tmp_path / "hella.log"
    write_log(log, [PROCESSED])
    db = tmp_path / "hella_gulps.sqlite"
    ledger = hella_log.HellaLogLedger(db, log, first_tail_bytes=0)
    ledger.tick()
    ledger.close()
    replacement = tmp_path / "next.log"
    write_log(replacement, [CAP, CAPPED])
    replacement.replace(log)
    ledger = hella_log.HellaLogLedger(db, log, first_tail_bytes=0)
    result = ledger.tick()
    # first_tail_bytes 0 means "start at EOF": a replaced file is not re-read.
    assert result["rows"] == 0 and result["offset"] == log.stat().st_size
    ledger.close()


def test_first_run_starts_at_size_minus_tail_budget(tmp_path):
    log = tmp_path / "hella.log"
    filler = "x" * 200
    write_log(log, [filler, CAP, CAPPED, PROCESSED])
    size = log.stat().st_size
    # Tail budget lands inside the filler line; that partial line is skipped.
    ledger = hella_log.HellaLogLedger(tmp_path / "hella_gulps.sqlite", log,
                                      first_tail_bytes=size - 100)
    result = ledger.tick()
    connection = ledger.connect()
    streams = [r[0] for r in connection.execute("SELECT stream FROM gulps ORDER BY stream")]
    assert streams == [4, 7] and result["offset"] == size
    ledger.close()


def test_missing_log_and_retention(tmp_path):
    ledger = hella_log.HellaLogLedger(tmp_path / "hella_gulps.sqlite", tmp_path / "absent.log")
    assert ledger.tick()["status"] == "missing_log"
    connection = ledger.connect()
    connection.execute("INSERT INTO gulps (ts_unix, stream, cap_hit) VALUES (1000, 0, 0)")
    connection.commit()
    log = tmp_path / "hella.log"
    write_log(log, [PROCESSED])
    ledger.log_path = log
    ledger.tick()
    assert connection.execute("SELECT count(*) FROM gulps WHERE ts_unix < 2000").fetchone()[0] == 0
    ledger.close()


def test_ledger_info_reports_watermark(tmp_path):
    db = tmp_path / "hella_gulps.sqlite"
    assert hella_log.ledger_info(db)["status"] == "missing_log"
    log = tmp_path / "hella.log"
    write_log(log, [PROCESSED])
    ledger = hella_log.HellaLogLedger(db, log)
    ledger.tick()
    ledger.close()
    info = hella_log.ledger_info(db)
    assert info["status"] == "ok" and info["watermark_offset"] == log.stat().st_size
    assert info["log_path"] == str(log) and info["last_tick_unix"] > 0
    assert hella_log.read_last_gulps(db, info["newest_unix"]) == {4: info["newest_unix"]}
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_workspace_app_starts_the_ledger(settings, store, tmp_path, monkeypatch):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from casm_monitor.web.app import create_app

    log = tmp_path / "hella.log"
    write_log(log, [PROCESSED, CHANSTAT])
    monkeypatch.setenv("CASM_MONITOR_WORKSPACE", "1")
    monkeypatch.setenv("CASM_MONITOR_HELLA_LOG", str(log))
    monkeypatch.setenv("CASM_MONITOR_HELLA_LOG_INTERVAL_S", "3600")
    root = tmp_path / "preview"
    app = create_app(replace(settings, observation_cache_root=root, t2_db=tmp_path / "absent.sqlite"),
                     read_only=True)
    with TestClient(app) as client:
        refresher = app.state.hella_ledger
        for _ in range(200):
            if (root / hella_log.LEDGER_NAME).is_file():
                break
            time.sleep(0.02)
        payload = client.get("/api/t1").json()
    refresher.stop()
    assert payload["ledger"]["log_path"] == str(log)
    assert [row["stream"] for row in payload["streams"]] == list(range(8))
    assert hella_log.ledger_info(root / hella_log.LEDGER_NAME)["status"] == "ok"


def test_ledger_not_started_without_workspace(settings, store, tmp_path, monkeypatch):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from casm_monitor.web.app import create_app

    log = tmp_path / "hella.log"
    write_log(log, [PROCESSED])
    monkeypatch.delenv("CASM_MONITOR_WORKSPACE", raising=False)
    monkeypatch.setenv("CASM_MONITOR_HELLA_LOG", str(log))
    root = tmp_path / "preview"
    app = create_app(replace(settings, observation_cache_root=root), read_only=True)
    with TestClient(app):
        pass
    assert not (root / hella_log.LEDGER_NAME).exists()
    assert not hasattr(app.state, "hella_ledger")


def test_default_tick_interval_is_one_minute(monkeypatch):
    monkeypatch.delenv("CASM_MONITOR_HELLA_LOG_INTERVAL_S", raising=False)
    assert hella_log.interval_from_env() == 60.0
    monkeypatch.setenv("CASM_MONITOR_HELLA_LOG_INTERVAL_S", "30")
    assert hella_log.interval_from_env() == 30.0
    monkeypatch.setenv("CASM_MONITOR_HELLA_LOG_INTERVAL_S", "nonsense")
    assert hella_log.interval_from_env() == 60.0
