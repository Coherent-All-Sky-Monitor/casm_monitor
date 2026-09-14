"""Existing emitted-candidate bins do not establish raw-peak saturation."""
import json
from datetime import datetime, timezone

from casm_monitor.collectors.search import DM_EDGES, WIDTH_EDGES, ensure_tables
from casm_monitor.store import Store
from casm_monitor.web import t1


def test_bounded_bins_and_dark_matplotlib(tmp_path, monkeypatch):
    store = Store(tmp_path / "monitor.sqlite")
    ensure_tables(store)
    for stamp, job, n in ((100, 0, 120), (101, 0, 80), (102, 4, 25)):
        dm = [n] + [0] * (len(DM_EDGES) - 2)
        width = [n] + [0] * (len(WIDTH_EDGES) - 2)
        beam = [n] + [0] * 63
        store.execute("INSERT INTO cand_bins VALUES (?,?,?,?,?,?,?,?,?)", (stamp, job, n, "[]", json.dumps(dm), json.dumps(width), json.dumps(beam), 15, 0))
    result = t1.build_t1(store, t0="90", t1="110", log_path=tmp_path / "absent.log")
    assert result["status"] == "ok"
    assert result["n_candidates"] == 225
    assert result["cap_fraction"] is None and result["skipped_beams"] is None
    assert "cluster peaks" in result["note"]
    assert sum(result["width_counts"]) == 225
    assert t1.render_t1(result).startswith(b"\x89PNG")
    from matplotlib.figure import Figure
    save = Figure.savefig
    captured = []
    def inspect(fig, *args, **kwargs):
        captured.append(fig)
        return save(fig, *args, **kwargs)
    monkeypatch.setattr(Figure, 'savefig', inspect)
    for zone in ('America/Los_Angeles','UTC'):
        t1.render_t1({**result,'time_tz':zone})
        fig = captured[-1]
        assert fig.axes[2].get_ylim() == (0,1000)
        assert fig.axes[0].get_xlabel() == ('UTC' if zone == 'UTC' else 'OVRO local (PDT/PST)')
    monkeypatch.setattr(t1, "MAX_ROWS", 1)
    partial = t1.build_t1(store, t0="90", t1="110", log_path=tmp_path / "absent.log")
    assert partial["status"] == "partial" and partial["n_candidates"] == 25
    store.close()


def test_only_explicit_log_warning_measures_processed_beams(tmp_path):
    path = tmp_path / "hella.log"
    path.write_text("2 [2026-09-13 12:00:00.000] [warning] Only processed 3/64 beams - detected 10000 peaks\n"
                    "2 [2026-09-13 12:00:01.000] [info] sending data\n")
    start = datetime(2026, 9, 13, 19, tzinfo=timezone.utc).timestamp()
    data = t1.log_evidence(path, start=start, end=start + 2)
    assert data["cap_warnings"][0]["processed_beams"] == 3
    assert data["cap_warnings"][0]["utc"].startswith("2026-09-13T19:00:00")
    assert not data["complete_window"]


def test_missing_bins_not_zero(tmp_path):
    store = Store(tmp_path / "monitor.sqlite")
    data = t1.build_t1(store, t0="90", t1="110", log_path=tmp_path / "missing.log")
    assert data["status"] == "unavailable"
    assert "n_candidates" not in data
    store.close()
