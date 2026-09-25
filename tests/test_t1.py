"""T1 page: stream liveness from the gulp ledger, candidates from cand_bins."""
import json

import pytest

from casm_monitor import hella_log
from casm_monitor.collectors.search import DM_EDGES, WIDTH_EDGES, ensure_tables
from casm_monitor.store import Store
from casm_monitor.web import t1

NOW = 1_780_000_000.0
SPAN = 86400.0


def make_ledger(path, *, end=NOW, silent_stream=4, silent_for_s=1200.0, cap_stream=None,
                tick_unix=None):
    """Eight live streams at the 8.59 s cadence; one stopped ``silent_for_s`` ago."""
    ledger = hella_log.HellaLogLedger(path, path.parent / "absent.log")
    connection = ledger.connect()
    rows = []
    for stream in range(8):
        newest = end - (silent_for_s if stream == silent_stream else 5.0)
        for index in range(420):
            stamp = newest - index * hella_log.GULP_S
            cap = 1 if (cap_stream == stream and index == 0) else 0
            rows.append((stamp, stream, 5.6, 0.12, 0.52, 3.7, 1.03, 0.17, cap, None, 472))
    connection.executemany("INSERT INTO gulps (ts_unix, stream, wall_s, read_s, flag_s, dedisp_s, "
                           "smooth_s, peak_s, cap_hit, beams_processed, blanked) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    ledger._set_watermark(connection, {"offset": 1234, "inode": 7, "size": 1234,
                                       "last_tick_unix": end if tick_unix is None else tick_unix,
                                       "log_path": "/data/casm/logs/bf_proc_hella.log"})
    connection.commit()
    ledger.close()
    return path


def add_cand_bins(store, entries):
    ensure_tables(store)
    for stamp, stream, n in entries:
        dm = [0, n] + [0] * (len(DM_EDGES) - 3)
        width = [n] + [0] * (len(WIDTH_EDGES) - 2)
        beam = [n] + [0] * 63
        store.execute("INSERT INTO cand_bins VALUES (?,?,?,?,?,?,?,?,?)",
                      (stamp, stream, n, "[]", json.dumps(dm), json.dumps(width),
                       json.dumps(beam), 15, 0))


@pytest.fixture
def payload(tmp_path):
    store = Store(tmp_path / "monitor.sqlite")
    ledger_db = make_ledger(tmp_path / "hella_gulps.sqlite", cap_stream=2)
    add_cand_bins(store, [(NOW - 100.0, 0, 120), (NOW - 108.6, 0, 80)])
    data = t1.build_t1(store, t0=str(NOW - SPAN), t1=str(NOW), ledger_db=ledger_db)
    store.close()
    return data


def test_stream_liveness_and_empty_gulps(payload):
    streams = payload["streams"]
    assert [row["stream"] for row in streams] == list(range(8))
    assert [row["node"] for row in streams] == ["corr1"] * 4 + ["corr2"] * 4
    assert [row["status"] for row in streams] == ["ok"] * 4 + ["silent"] + ["ok"] * 3
    assert streams[4]["last_gulp_age_s"] == pytest.approx(1200.0)
    assert streams[0]["gulps_last_hour"] == 419
    assert streams[0]["expected_gulps_per_hour"] == pytest.approx(3600 / 8.59)
    assert streams[0]["median_wall_s_last_hour"] == pytest.approx(5.6)
    # Two of stream 0's gulps emitted candidates, the rest were empty.
    assert streams[0]["empty_fraction_last_hour"] == pytest.approx((419 - 2) / 419)
    assert streams[1]["empty_fraction_last_hour"] == 1.0
    assert streams[2]["cap_hits_last_hour"] == 1
    assert sum(row["cap_hits_last_hour"] for row in streams) == 1


def test_age_is_measured_from_the_last_log_read(tmp_path):
    store = Store(tmp_path / "monitor.sqlite")
    ensure_tables(store)
    # The ledger was read 300 s ago; a stream whose newest gulp was 5 s before
    # that read is healthy, not 305 s "late".
    ledger_db = make_ledger(tmp_path / "hella_gulps.sqlite", tick_unix=NOW - 300.0)
    data = t1.build_t1(store, t0=str(NOW - SPAN), t1=str(NOW), ledger_db=ledger_db)
    streams = data["streams"]
    assert streams[0]["last_gulp_age_s"] == 0.0 and streams[0]["status"] == "ok"
    assert streams[4]["last_gulp_age_s"] == pytest.approx(900.0)
    assert streams[4]["status"] == "silent"
    assert data["ledger"]["last_tick_unix"] == NOW - 300.0
    assert data["ledger"]["read_age_s"] > 0
    store.close()


def test_ledger_block_and_activity_states(payload):
    ledger = payload["ledger"]
    assert ledger["status"] == "ok" and ledger["rows_in_window"] == 8 * 420
    assert ledger["watermark_offset"] == 1234 and ledger["last_tick_unix"] == NOW
    assert ledger["log_path"].endswith("bf_proc_hella.log")
    gulps = payload["activity"]["gulps"]
    cands = payload["activity"]["cands"]
    emitting = payload["activity"]["gulps_with_cands"]
    caps = payload["activity"]["cap_hits"]
    assert len(gulps) == t1.TIME_BINS and len(gulps[0]) == 8
    assert payload["time_bin_seconds"] == pytest.approx(180.0)
    assert sum(sum(bin_) for bin_ in gulps) == 8 * 420
    assert sum(sum(bin_) for bin_ in cands) == 200 and payload["n_candidates"] == 200
    assert sum(sum(bin_) for bin_ in caps) == 1
    # The last bin holds both candidate gulps on stream 0.
    last = t1.TIME_BINS - 1
    assert cands[last][0] == 200 and gulps[last][0] > 0
    assert emitting[last][0] == 2 and sum(sum(bin_) for bin_ in emitting) == 2
    assert emitting[last][0] < gulps[last][0]
    assert payload["quiet_bins"][last] is False
    # Bins where streams ran but emitted nothing are quiet, not missing.
    quiet = [i for i, flag in enumerate(payload["quiet_bins"]) if flag]
    assert quiet and last not in quiet
    assert all(sum(gulps[i]) > 0 for i in quiet)


def test_histograms_and_removed_fields(payload):
    assert payload["status"] == "ok" and payload["n_bin_rows"] == 2
    assert sum(payload["width_counts"]) == 200
    assert payload["dm_counts"][1] == 200 and payload["dm_counts"][0] == 0
    assert len(payload["dm_time_counts"][0]) == len(DM_EDGES) - 1
    assert len(payload["beam_time_counts"][0]) == 512
    assert payload["refresh_s"] == 300
    for gone in ("log", "per_job_counts", "per_job_peak_per_gulp", "observed_instance_gulps",
                 "cap_fraction", "skipped_beams", "raw_peak_cap", "display_dm_max",
                 "display_note", "coverage_note", "note"):
        assert gone not in payload


def test_render_full_payload(payload, monkeypatch):
    from matplotlib.figure import Figure

    captured = []
    save = Figure.savefig

    def inspect(fig, *args, **kwargs):
        captured.append(fig)
        return save(fig, *args, **kwargs)

    monkeypatch.setattr(Figure, "savefig", inspect)
    for zone in ("America/Los_Angeles", "UTC"):
        assert t1.render_t1({**payload, "time_tz": zone}).startswith(b"\x89PNG")
        axes = captured[-1].axes
        # activity, activity cbar, beam, beam cbar, DM, DM cbar, width, DM hist
        assert axes[0].get_xlabel() == ("Time (UTC)" if zone == "UTC" else "Time (OVRO local · PDT/PST)")
        assert axes[0].get_ylabel() == "Stream ID"
        assert axes[2].get_ylabel() == "Beam index"
        assert axes[4].get_yscale() == "log" and axes[7].get_xscale() == "log"
        assert "1 gulp ≈ 8.59 s" in axes[0].get_title()
        assert axes[1].get_ylabel() == "candidates per stream\nper 3 min (log scale)"
        assert axes[5].get_ylabel() == "candidates per DM bin\nper 3 min (log scale)"
        assert axes[0].collections[1].get_array().compressed().sum() == 200
        assert axes[4].collections[1].get_array().compressed().sum() == 200
        from matplotlib.colors import to_rgba

        for index in (0, 2, 4):
            assert axes[index].get_facecolor() == to_rgba(t1.MISSING_COLOR)
            assert axes[index].collections[0].cmap(0) == to_rgba(t1.EMPTY_COLOR)
            assert axes[index].collections[1].norm is axes[0].collections[1].norm
            assert axes[index].collections[1].cmap is axes[0].collections[1].cmap
            assert axes[index].collections[1].norm.vmax == 200
            assert list(axes[index + 1].get_yticks()) == [1, 10, 100, 200]
        assert all("healthy" not in text.get_text() for text in axes[0].get_legend().get_texts())

    assert t1.render_t1({**payload, "time_bin_seconds": 7.5}).startswith(b"\x89PNG")
    axes = captured[-1].axes
    assert axes[1].get_ylabel() == "candidates per stream\nper 7.5 s (log scale)"
    assert axes[3].get_ylabel() == "candidates per beam\nper 7.5 s (log scale)"
    assert axes[5].get_ylabel() == "candidates per DM bin\nper 7.5 s (log scale)"

    from copy import deepcopy

    near_decade = deepcopy(payload)
    near_decade["activity"]["cands"][-1][0] = 10001
    assert t1.render_t1(near_decade).startswith(b"\x89PNG")
    figure = captured[-1]
    for index in (1, 3, 5):
        axis = figure.axes[index]
        assert list(axis.get_yticks()) == [1, 10, 100, 1000, 10001]
        assert axis.get_yticklabels()[-1].get_text() == "10,001"
        boxes = [label.get_window_extent(figure.canvas.get_renderer())
                 for label in axis.get_yticklabels()]
        assert all(lower.y1 + 4 < upper.y0 for lower, upper in zip(boxes, boxes[1:]))


def test_white_figure_axes_counts_and_style_isolation(payload, monkeypatch):
    import io
    from copy import deepcopy
    from PIL import Image
    from matplotlib import rc_context, rcParams
    from matplotlib.colors import to_rgba
    from matplotlib.figure import Figure

    before = deepcopy(payload)
    captured = []
    save = Figure.savefig
    def inspect(fig,*args,**kwargs):
        captured.append(fig)
        return save(fig,*args,**kwargs)
    monkeypatch.setattr(Figure,'savefig',inspect)
    with rc_context({'text.color':'white','axes.labelcolor':'white','font.size':18}):
        content=t1.render_t1(payload)
        assert rcParams['text.color']=='white' and rcParams['font.size']==18
    assert payload==before
    fig=captured[-1]
    assert fig.get_facecolor()==to_rgba('white')
    assert fig._suptitle.get_text().startswith('Search (T1) · Hella')
    assert Image.open(io.BytesIO(content)).convert('RGB').getpixel((0,0))==(255,255,255)
    axes=fig.axes
    assert axes[6].get_facecolor()==axes[7].get_facecolor()==to_rgba('white')
    # Ordered counts get darker on white; caps and missing/zero retain their colours.
    import numpy as np
    cmap=axes[0].collections[1].cmap
    assert cmap(0.)==to_rgba('#ded3ee')
    assert cmap(1.)==to_rgba('#39135f')
    rgb=cmap(np.linspace(0,1,256))[:,:3]
    linear=np.where(rgb<=.04045,rgb/12.92,((rgb+.055)/1.055)**2.4)
    assert np.all(np.diff(linear @ [0.2126,0.7152,0.0722])<0)
    assert all(p.get_facecolor()==to_rgba('#456a9a') for ax in axes[6:8] for p in ax.patches)
    assert axes[0].collections[2].cmap(0.)==to_rgba('#c53030')
    assert list(axes[2].get_yticks())==list(range(0,513,64))
    assert list(axes[4].get_yticks())==[10,30,100,300,1000,3000]
    assert list(axes[7].get_xticks())==[10,30,100,300,1000,3000]
    assert axes[6].get_ylabel()==axes[7].get_ylabel()=='Candidates'
    assert axes[6].get_xlabel()=='Hella width index (not FWHM)'
    assert axes[7].get_xlabel()=='DM (pc cm⁻³)'
    for index in (0,2,4):
        assert axes[index].collections[1].get_array().compressed().sum()==200
        assert axes[index].get_xlim()==axes[0].get_xlim()
    assert sum(p.get_height() for p in axes[6].patches)==200
    assert axes[7].patches[0].get_data().values.sum()==200
    assert sum(mesh.get_array().compressed().sum() for mesh in axes[0].collections[2:])==1
    # savefig temporarily uses 150 dpi; redraw at the restored figure dpi
    # before comparing artist bounds with the current canvas dimensions.
    fig.canvas.draw()
    renderer=fig.canvas.get_renderer()
    for ax in axes:
        for labels,direction in ((ax.get_xticklabels(),'x'),(ax.get_yticklabels(),'y')):
            boxes=[label.get_window_extent(renderer) for label in labels if label.get_visible() and label.get_text()]
            boxes.sort(key=lambda b:getattr(b,direction+'0'))
            assert all(getattr(a,direction+'1')+2<getattr(b,direction+'0') for a,b in zip(boxes,boxes[1:]))
    for ax in axes:
        if not ax.axison:
            continue
        for label in [ax.title,ax.xaxis.label,ax.yaxis.label]:
            if label.get_text():
                box=label.get_window_extent(renderer)
                assert box.x0>=0 and box.y0>=0 and box.x1<=fig.bbox.width and box.y1<=fig.bbox.height


def test_empty_ledger_and_empty_cand_bins(tmp_path):
    store = Store(tmp_path / "monitor.sqlite")
    ensure_tables(store)
    empty = t1.build_t1(store, t0=str(NOW - SPAN), t1=str(NOW),
                        ledger_db=tmp_path / "absent.sqlite")
    assert empty["status"] == "empty" and empty["ledger"]["status"] == "missing_log"
    assert all(row["status"] == "unknown" for row in empty["streams"])
    assert not any(empty["quiet_bins"])
    assert t1.render_t1(empty).startswith(b"\x89PNG")

    ledger_db = make_ledger(tmp_path / "hella_gulps.sqlite")
    quiet = t1.build_t1(store, t0=str(NOW - SPAN), t1=str(NOW), ledger_db=ledger_db)
    assert quiet["status"] == "empty" and quiet["n_candidates"] == 0
    assert any(quiet["quiet_bins"])
    assert t1.render_t1(quiet).startswith(b"\x89PNG")
    store.close()


def test_compact_activity_uses_the_full_plot_count_scale(payload, monkeypatch):
    import io
    from PIL import Image
    from matplotlib.figure import Figure
    captured=[]
    save=Figure.savefig
    def inspect(fig,*args,**kwargs):
        captured.append(fig)
        return save(fig,*args,**kwargs)
    monkeypatch.setattr(Figure,'savefig',inspect)
    t1.render_t1(payload)
    full=captured[-1]
    png=t1.render_t1(payload,compact=True)
    compact=captured[-1]
    assert len(compact.axes)==2
    assert Image.open(io.BytesIO(png)).size==(1350,570)
    a,b=full.axes[0],compact.axes[0]
    assert (a.collections[1].get_array()==b.collections[1].get_array()).all()
    assert a.collections[1].norm.vmin==b.collections[1].norm.vmin
    assert a.collections[1].norm.vmax==b.collections[1].norm.vmax
    assert a.collections[1].cmap(.5)==b.collections[1].cmap(.5)
    assert a.get_ylabel()==b.get_ylabel()=='Stream ID'
    assert sum(mesh.get_array().compressed().sum() for mesh in b.collections[2:])==1


def test_live_status_does_not_render_or_read_candidate_arrays(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from casm_monitor.config import Settings
    def forbidden(*args, **kwargs):
        raise AssertionError('Live status must not build or render the history plots')
    monkeypatch.setattr(t1,'build_t1',forbidden)
    monkeypatch.setattr(t1,'render_t1',forbidden)
    app=FastAPI()
    app.include_router(t1.build_router(None,Settings(observation_cache_root=tmp_path)))
    with TestClient(app) as client:
        payload=client.get('/api/t1/status').json()
    assert len(payload['streams'])==8
    assert all(s['status']=='unknown' for s in payload['streams'])
    assert payload['ledger']['last_tick_unix'] is None
    assert all('empty_fraction_last_hour' not in s for s in payload['streams'])


def test_missing_cand_bins_table_still_reports_streams(tmp_path):
    store = Store(tmp_path / "monitor.sqlite")
    ledger_db = make_ledger(tmp_path / "hella_gulps.sqlite")
    data = t1.build_t1(store, t0=str(NOW - SPAN), t1=str(NOW), ledger_db=ledger_db)
    assert data["status"] == "unavailable" and "n_candidates" not in data
    assert [row["status"] for row in data["streams"]][4] == "silent"
    assert t1.render_t1(data).startswith(b"\x89PNG")
    store.close()


def test_row_budget_marks_partial(tmp_path, monkeypatch):
    store = Store(tmp_path / "monitor.sqlite")
    add_cand_bins(store, [(NOW - 100.0, 0, 120), (NOW - 108.6, 0, 80)])
    monkeypatch.setattr(t1, "MAX_ROWS", 1)
    data = t1.build_t1(store, t0=str(NOW - SPAN), t1=str(NOW),
                       ledger_db=tmp_path / "absent.sqlite")
    assert data["status"] == "partial" and data["n_candidates"] == 120
    store.close()
