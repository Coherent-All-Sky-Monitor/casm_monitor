"""Recovery denominators, incomplete evidence and archive isolation."""

from datetime import datetime, timedelta, timezone
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from casm_monitor.config import Settings
from casm_monitor.web import injection_overview as overview

NOW = datetime(2026, 9, 13, 23, tzinfo=timezone.utc)


def ledger(tmp_path, rows):
    path = tmp_path / "t2.sqlite"
    columns = overview.FIELDS.split(",")
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE injections (" + ",".join(columns) + ")")
        connection.execute("CREATE INDEX idx_injections_utc ON injections(inject_utc)")
        for i, row in enumerate(rows):
            record = dict.fromkeys(columns)
            record.update(id=i, file_id=f"inj_20260913_{i:04d}",
                          inject_utc=f"2026-09-13T{12+i:02d}:00:00+00:00",
                          sigma_ms=10.0, rec_width=5)
            record.update(row)
            connection.execute("INSERT INTO injections VALUES (" + ",".join("?" for _ in columns) + ")",
                               [record[column] for column in columns])
    return Settings(t2_db=path, store_root=tmp_path / "store")


def test_denominator_and_missing_trial_evidence(tmp_path):
    settings = ledger(tmp_path, [
        {"outcome": "recovered", "gate_trigger": 0},
        {"outcome": "missed_t1", "n_t1_trials": None},
        {"outcome": "missed_t2", "n_t1_trials": 3},
        {"outcome": "fire_failed"}, {"outcome": None}, {"outcome": "old_category"},
        {"outcome": "recovered", "inject_utc": "2026-09-12T23:00:00+00:00"},
        {"outcome": "recovered", "inject_utc": "2026-09-01T23:00:00+00:00"},
    ])
    before = settings.t2_db.read_bytes()
    result = overview.build_injection_overview(settings, now=NOW, events_root=tmp_path / "events")
    assert result["status"] == "ok"
    assert result["counts"]["completed_fired"] == 4
    assert result["counts"]["recovery_fraction"] == 1 / 2
    assert result["window_start_utc"] == "2026-09-12T23:00:00+00:00"
    assert len(result["misses"]) == 2
    assert result["counts"]["pending"] == result["counts"]["unknown"] == 1
    assert result["latest_completed"]["outcome"] == "fire_failed"
    assert len(result["trend"]) == 7
    assert result["trend"][-2]["recovered"] == 1
    miss = next(row for row in result["recent"] if row["outcome"] == "missed_t1")
    assert "does not demonstrate zero trials" in miss["evidence_note"]
    recovered = next(row for row in result["recent"] if row["outcome"] == "recovered")
    assert recovered["recovered_fwhm_ms"] < 33.6
    assert not recovered["replay"]["available"]
    assert settings.t2_db.read_bytes() == before


def test_absent_database_is_not_created(tmp_path):
    settings = Settings(t2_db=tmp_path / "missing.sqlite")
    result = overview.build_injection_overview(settings, now=NOW)
    assert result["status"] == "unavailable"
    assert not result["counts_complete"]
    assert not settings.t2_db.exists()


def test_selected_interval_timeline_is_not_the_recent_row_sample(tmp_path):
    settings = ledger(tmp_path, [{"outcome":"recovered", "inject_utc":(NOW-timedelta(minutes=i)).isoformat()} for i in range(45)])
    before = settings.t2_db.read_bytes()
    result = overview.build_injection_overview(settings, now=NOW,
        t0=(NOW-timedelta(minutes=44)).timestamp(), t1=NOW.timestamp(), events_root=tmp_path/'events')
    assert result['counts']['recovered']==45
    assert len(result['recent'])==30 and len(result['trials'])==45
    assert all('replay_png' not in shot for shot in result['trials'])
    assert settings.t2_db.read_bytes()==before
    result = overview.build_injection_overview(settings, now=NOW,
        t0=(NOW-timedelta(minutes=40)).timestamp(), t1=(NOW-timedelta(minutes=30)).timestamp())
    assert result['counts']['recovered']==len(result['trials'])==11
    assert result['window_end_utc']==(NOW-timedelta(minutes=30)).isoformat()
    assert result['as_of_utc']==NOW.isoformat()


def test_historical_window_and_trend_end_on_the_selected_date(tmp_path):
    earlier=NOW-timedelta(days=3)
    settings=ledger(tmp_path,[{'outcome':'recovered','inject_utc':earlier.isoformat()}, {'outcome':'missed_t1'}])
    result=overview.build_injection_overview(settings,now=NOW,
        t0=(earlier-timedelta(days=7)).timestamp(),t1=earlier.timestamp())
    assert result['counts']['recovered']==1 and result['counts']['missed_t1']==0
    assert result['trend'][-1]['date_utc']==earlier.date().isoformat()
    assert len(result['trend'])==7
    app=FastAPI();app.include_router(overview.build_router(settings))
    with TestClient(app) as client:
        assert client.get('/api/observation/injections',params={'t0':earlier.timestamp()-3600,'t1':earlier.timestamp()}).json()['counts']['recovered']==1
        for bounds in [('bad','1'),('nan','1'),('1','inf'),('2','1'),('0',str(8*86400))]:
            assert client.get('/api/observation/injections',params=dict(zip(('t0','t1'),bounds))).status_code==400


def test_row_budget_explicitly_marks_counts_incomplete(tmp_path, monkeypatch):
    settings = ledger(tmp_path, [{"outcome": "recovered"}] * 3)
    monkeypatch.setattr(overview, "MAX_ROWS", 2)
    result = overview.build_injection_overview(settings, now=NOW, events_root=tmp_path / "events")
    assert result["status"] == "partial"
    assert not result["counts_complete"]
    assert len(result["recent"]) == 2


def test_safe_saved_artifacts_and_no_arbitrary_ledger_paths(tmp_path):
    root = tmp_path / "events"
    shot_dir = root / "inj_20260913_0000"
    shot_dir.mkdir(parents=True)
    png = shot_dir / "inj_20260913_0000.png"
    png.write_bytes(b"saved png")
    outside = tmp_path / "outside.json"
    outside.write_text("private")
    (shot_dir / "inj_20260913_0000.json").symlink_to(outside)
    settings = ledger(tmp_path, [{"outcome": "recovered", "replay_png": str(outside)}])
    result = overview.build_injection_overview(settings, now=NOW, events_root=root)
    replay = result["latest_completed"]["replay"]
    assert replay["artifacts"]["png"]["available"]
    assert not replay["artifacts"]["json"]["available"]
    app = FastAPI()
    app.include_router(overview.build_router(settings, events_root=root))
    with TestClient(app) as client:
        response = client.get(replay["artifacts"]["png"]["url"])
        assert response.status_code == 200
        assert response.content == b"saved png"
        for target in ("inj_20260913_0000/json", "inj_20260913_0000/py", "outside/png", "../outside/json"):
            assert client.get("/api/observation/injections/artifact/" + target).status_code == 404
