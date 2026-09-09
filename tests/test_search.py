"""Search backend (M2b): the cands tailer, its binning and the Search API.

Everything runs against synthetic files and a tmp_path store; no test touches
/mnt, the live cands files, corr2 or the t2 database (the corr2 leg is driven
through the collector's injectable ssh runner).
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from casm_monitor.collectors import search as search_mod
from casm_monitor.collectors.base import CollectorContext
from casm_monitor.collectors.search import (
    DM_EDGES,
    JOBS_CORR2,
    SNR_EDGES,
    TSAMP_S,
    WIDTH_EDGES,
    Bin,
    SearchCollector,
    SearchConfig,
    bin_cands,
    cands_path,
    corr2_command,
    find_current_obs,
    gulp_ts,
    hist_index,
    histogram,
    obs_and_job,
    parse_chunk,
    parse_line,
    read_gulp_stats,
    split_job_blocks,
)
from casm_monitor.config import Settings
from casm_monitor.store import ShardWriter, Store
from casm_monitor.util import parse_iso, utc_start_to_unix
from casm_monitor.web.search import build_router, field_edges, step_and_bins, window

OBS = "2026-09-04-16:42:39"
OBS_UNIX = utc_start_to_unix(OBS)
HEADER = "SNR SAMP_START TIME_START WIDTH DM_IDX DM BEAM_IDX\n"


def row(snr: float, samp: int, width: int, dm_idx: int, dm: float, beam: int) -> str:
    days = samp * TSAMP_S / 86400.0
    return f"{snr} {samp} {days:.6f} {width} {dm_idx} {dm} {beam}\n"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        store_root=tmp_path / "store",
        hella_cands_dir=tmp_path / "cands",
        t2_db=tmp_path / "t2.sqlite",
        cadences={"search": 20.0},
    )


@pytest.fixture
def store(settings: Settings) -> Store:
    st = Store(settings.db_path, store_root=settings.store_root)
    yield st
    st.close()


@pytest.fixture
def ctx(settings: Settings, store: Store) -> CollectorContext:
    return CollectorContext(
        settings=settings, store=store, shards=ShardWriter(store, settings.shards_root)
    )


def write_file(settings: Settings, obs: str, job: int, text: str, *, mode: str = "w") -> Path:
    path = Path(cands_path(settings.hella_cands_dir, obs, job))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode) as handle:
        handle.write(text)
    return path


def fake_ssh(files: dict[int, Path], *, fail: bool = False):
    """A stand-in for ssh: serves local files with the real marker protocol."""
    calls: list[str] = []

    def runner(host: str, command: str, timeout: float):
        calls.append(command)
        if fail:
            return 255, b"", b"ssh: connect to host casm-corr2 port 22: No route to host"
        out = bytearray()
        for job_s, off_s in re.findall(r'"(\d+):(-?\d+)"', command):
            job, offset = int(job_s), int(off_s)
            path = files.get(job)
            if path is None or not path.is_file():
                out += f"=== {job} -1\n".encode()
                continue
            data = path.read_bytes()
            out += f"=== {job} {len(data)}\n".encode()
            out += data[-abs(offset):] if offset < 0 else data[offset:]
        return 0, bytes(out), b""

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


# -- parsing ---------------------------------------------------------------
def test_obs_and_job_and_paths() -> None:
    assert obs_and_job(f"cands_{OBS}.dat.7") == (OBS, 7)
    assert obs_and_job("/x/y/cands_2026-09-04-16:42:39.dat.0") == (OBS, 0)
    assert obs_and_job("cands_garbage.dat.0") is None
    assert obs_and_job("hella_0.cfg") is None
    assert cands_path("/tmp", OBS, 3) == f"/tmp/cands_{OBS}.dat.3"


def test_find_current_obs_picks_the_newest_by_mtime(settings) -> None:
    import os

    old = write_file(settings, OBS, 0, HEADER)
    new = write_file(settings, "2026-09-08-01:00:00", 0, HEADER)
    now = time.time()
    os.utime(old, (now, now))
    os.utime(new, (now + 30, now + 30))
    assert find_current_obs(settings.hella_cands_dir) == "2026-09-08-01:00:00"
    # A higher-sorting name with an older mtime does not win.
    os.utime(new, (now - 600, now - 600))
    assert find_current_obs(settings.hella_cands_dir) == OBS
    assert find_current_obs(settings.hella_cands_dir / "nope") is None


def test_parse_line_columns_and_time() -> None:
    cand = parse_line("26.0045 8306705 0.100813 6 2 21.0372 5", job=0, node="corr1", utc_start_unix=OBS_UNIX)
    assert cand is not None
    assert (cand.snr, cand.samp, cand.width, cand.dm_idx, cand.dm, cand.beam) == (
        26.0045,
        8306705,
        6,
        2,
        21.0372,
        5,
    )
    assert cand.ts_unix == pytest.approx(OBS_UNIX + 8306705 * TSAMP_S)
    # the header line, a short line and an out-of-range beam are all rejected
    assert parse_line(HEADER.strip(), job=0, node="corr1", utc_start_unix=OBS_UNIX) is None
    assert parse_line("1 2 3", job=0, node="corr1", utc_start_unix=OBS_UNIX) is None
    assert parse_line(row(16, 10, 3, 4, 50.0, 512), job=0, node="corr1", utc_start_unix=OBS_UNIX) is None


def test_parse_chunk_keeps_partial_trailing_line() -> None:
    text = HEADER + row(16.0, 0, 3, 4, 50.0, 1) + row(17.0, 8192, 4, 5, 60.0, 2)
    partial = "18.5 16384 0.0002 5"
    chunk = (text + partial).encode()
    cands, consumed, n_bad = parse_chunk(chunk, job=0, node="corr1", utc_start_unix=OBS_UNIX)
    assert [c.snr for c in cands] == [16.0, 17.0]
    assert n_bad == 1  # the header
    # The watermark stops at the last newline, so the partial row is re-read.
    assert consumed == len(text.encode())
    assert chunk[consumed:].decode() == partial
    # No newline at all -> nothing is consumed and nothing is parsed.
    assert parse_chunk(b"18.5 16384", job=0, node="corr1", utc_start_unix=OBS_UNIX) == ([], 0, 0)


def test_parse_chunk_drops_first_partial_line_on_backfill() -> None:
    chunk = ("6.0045 8306705 0.1 6 2 21.0\n" + row(16.0, 8192, 3, 4, 50.0, 1)).encode()
    cands, consumed, _bad = parse_chunk(
        chunk, job=0, node="corr1", utc_start_unix=OBS_UNIX, drop_first_partial=True
    )
    assert [c.snr for c in cands] == [16.0]
    assert consumed == len(chunk)


def test_split_job_blocks_marker_protocol() -> None:
    stream = (
        b"=== 4 120\n" + row(16.0, 0, 3, 4, 50.0, 256).encode()
        + b"=== 5 -1\n"
        + b"=== 6 40\n" + row(20.0, 8192, 4, 5, 60.0, 400).encode()
    )
    blocks = split_job_blocks(stream)
    assert set(blocks) == {4, 5, 6}
    assert blocks[4][0] == 120 and blocks[4][1].startswith(b"16.0 0 ")
    assert blocks[5] == (-1, b"")
    assert blocks[6][1].split()[6] == b"400"


def test_corr2_command_shape() -> None:
    cmd = corr2_command("/mnt/nvme4/data/casm/hella_cands", OBS, {4: 100, 5: 0, 6: -16, 7: 12})
    assert cmd.count("=== $j") == 2  # the size marker and the absent-file marker
    assert '"4:100"' in cmd and '"6:-16"' in cmd
    assert "tail -c +$((o+1))" in cmd and 'tail -c "${o#-}"' in cmd
    assert f"cands_{OBS}.dat." in cmd


# -- binning ---------------------------------------------------------------
def test_hist_index_edges_and_clipping() -> None:
    assert hist_index(15.0, SNR_EDGES) == 0
    assert hist_index(1.0, SNR_EDGES) == 0            # below range -> first bin
    assert hist_index(1e9, SNR_EDGES) == len(SNR_EDGES) - 2  # above -> last bin
    assert SNR_EDGES[0] == 15.0 and SNR_EDGES[-1] == pytest.approx(100.0)
    assert DM_EDGES[0] == 0.0 and DM_EDGES[-1] == pytest.approx(3000.0)
    assert WIDTH_EDGES == [float(i) for i in range(10)]
    assert hist_index(6.0, WIDTH_EDGES) == 6
    # every value lands in exactly one bin: the counts sum to the input size
    values = [15.0, 20.0, 33.0, 99.9, 1000.0]
    assert sum(histogram(values, SNR_EDGES)) == len(values)


def test_gulp_ts_and_bin_cands() -> None:
    assert gulp_ts(0, OBS_UNIX) == OBS_UNIX
    assert gulp_ts(8191, OBS_UNIX) == OBS_UNIX
    assert gulp_ts(8192, OBS_UNIX) == pytest.approx(OBS_UNIX + 8192 * TSAMP_S)
    cands = [
        parse_line(row(16.0, 10, 3, 4, 50.0, 5), job=0, node="corr1", utc_start_unix=OBS_UNIX),
        parse_line(row(40.0, 20, 6, 9, 900.0, 7), job=0, node="corr1", utc_start_unix=OBS_UNIX),
        parse_line(row(18.0, 9000, 3, 4, 50.0, 5), job=0, node="corr1", utc_start_unix=OBS_UNIX),
    ]
    bins = bin_cands([c for c in cands if c], OBS_UNIX)
    assert len(bins) == 2
    first = bins[(round(OBS_UNIX, 6), 0)]
    assert first.n == 2
    assert first.snr_max == 40.0 and first.dm_at_snr_max == 900.0
    assert first.beam_counts[5] == 1 and first.beam_counts[7] == 1
    assert first.width_hist[3] == 1 and first.width_hist[6] == 1
    assert sum(first.snr_hist) == 2 and sum(first.dm_hist) == 2


def test_bin_merge_accumulates() -> None:
    a, b = Bin(), Bin()
    cand = parse_line(row(16.0, 10, 3, 4, 50.0, 5), job=0, node="corr1", utc_start_unix=OBS_UNIX)
    other = parse_line(row(80.0, 20, 4, 5, 60.0, 6), job=0, node="corr1", utc_start_unix=OBS_UNIX)
    a.add(cand)
    b.add(other)
    a.merge(b)
    assert a.n == 2 and a.snr_max == 80.0 and a.dm_at_snr_max == 60.0
    assert a.beam_counts[5] == 1 and a.beam_counts[6] == 1


# -- the collector ---------------------------------------------------------
def make_collector(settings: Settings, runner) -> SearchCollector:
    return SearchCollector(
        settings,
        config=SearchConfig(
            cands_dir=settings.hella_cands_dir,
            t2_db=settings.t2_db,
            backfill_hours=0.0,  # synthetic rows are dated in the obs's past
            retention_interval_s=1e9,
        ),
        ssh_runner=runner,
    )


def test_tick_ingests_both_nodes_and_watermarks_advance(settings, store, ctx) -> None:
    for job in (0, 1, 2, 3):
        write_file(settings, OBS, job, HEADER + row(16.0 + job, 100 + job, 3, 4, 50.0, 64 * job + 1))
    corr2_files = {
        job: write_file(settings, OBS, job, HEADER + row(20.0, 200, 4, 5, 60.0, 64 * job + 2))
        for job in JOBS_CORR2
    }
    runner = fake_ssh(corr2_files)
    collector = make_collector(settings, runner)

    collector.collect(ctx)
    assert len(runner.calls) == 1  # ONE ssh per tick for all four corr2 jobs
    rows = store.query("SELECT job, node, snr, beam FROM cands ORDER BY job")
    assert [int(r["job"]) for r in rows] == [0, 1, 2, 3, 4, 5, 6, 7]
    assert [r["node"] for r in rows] == ["corr1"] * 4 + ["corr2"] * 4
    assert store.latest_scalar("search.corr2_ok")["value"] == 1
    assert store.latest_scalar("search.obs")["value"] == OBS
    sizes = {
        job: Path(cands_path(settings.hella_cands_dir, OBS, job)).stat().st_size
        for job in range(8)
    }
    for job, size in sizes.items():
        assert store.get_watermark("search", f"file.{job}") == {"obs": OBS, "offset": size}

    # A second tick with no new bytes inserts nothing.
    collector.collect(ctx)
    assert store.query("SELECT COUNT(*) AS n FROM cands")[0]["n"] == 8

    # Append one row on each node: only the new bytes are ingested.
    write_file(settings, OBS, 0, row(31.0, 9000, 5, 6, 70.0, 3), mode="a")
    write_file(settings, OBS, 4, row(32.0, 9000, 5, 6, 70.0, 260), mode="a")
    collector.collect(ctx)
    assert sorted(
        float(r["snr"]) for r in store.query("SELECT snr FROM cands WHERE snr > 30")
    ) == [31.0, 32.0]
    assert store.query("SELECT COUNT(*) AS n FROM cands")[0]["n"] == 10


def test_partial_last_line_is_completed_next_tick(settings, store, ctx) -> None:
    write_file(settings, OBS, 0, HEADER + row(16.0, 100, 3, 4, 50.0, 1) + "17.5 8300 0.0001 4")
    collector = make_collector(settings, fake_ssh({}))
    collector.collect(ctx)
    assert store.query("SELECT COUNT(*) AS n FROM cands")[0]["n"] == 1
    # Finish the row; the watermark pointed at its first byte, so it is complete
    # exactly once (not duplicated, not lost).
    write_file(settings, OBS, 0, " 5 55.5 9\n", mode="a")
    collector.collect(ctx)
    snrs = sorted(float(r["snr"]) for r in store.query("SELECT snr FROM cands"))
    assert snrs == [16.0, 17.5]


def test_obs_rollover_resets_watermarks_and_emits_event(settings, store, ctx) -> None:
    write_file(settings, OBS, 0, HEADER + row(16.0, 100, 3, 4, 50.0, 1))
    collector = make_collector(settings, fake_ssh({}))
    collector.collect(ctx)
    old_offset = store.get_watermark("search", "file.0")["offset"]

    new_obs = "2026-09-08-01:00:00"
    path = write_file(settings, new_obs, 0, HEADER + row(19.0, 50, 2, 3, 40.0, 2))
    import os

    now = time.time()
    os.utime(path, (now + 10, now + 10))
    collector.collect(ctx)

    assert store.latest_scalar("search.obs")["value"] == new_obs
    watermark = store.get_watermark("search", "file.0")
    assert watermark["obs"] == new_obs
    assert watermark["offset"] == path.stat().st_size
    assert old_offset > 0  # the pre-rollover watermark is gone, not reused
    kinds = [e["kind"] for e in store.events(kind="search_obs_changed")]
    assert kinds == ["search_obs_changed"]
    # The new obs's row is ingested and the old rows are still there (7 d TTL).
    assert sorted(float(r["snr"]) for r in store.query("SELECT snr FROM cands")) == [16.0, 19.0]


def test_corr2_unreachable_is_reported_not_fatal(settings, store, ctx) -> None:
    write_file(settings, OBS, 0, HEADER + row(16.0, 100, 3, 4, 50.0, 1))
    for job in JOBS_CORR2:
        write_file(settings, OBS, job, HEADER + row(20.0, 200, 4, 5, 60.0, 64 * job + 2))
    collector = make_collector(settings, fake_ssh({}, fail=True))
    collector.collect(ctx)
    assert store.latest_scalar("search.corr2_ok")["value"] == 0
    # corr1 still ingested; no corr2 rows and no corr2 watermark.
    assert store.query("SELECT COUNT(*) AS n FROM cands")[0]["n"] == 1
    assert store.get_watermark("search", "file.4") is None

    # Recovery flips the scalar and leaves exactly one state event per change.
    corr2_files = {
        job: Path(cands_path(settings.hella_cands_dir, OBS, job)) for job in JOBS_CORR2
    }
    collector._ssh = fake_ssh(corr2_files)
    collector.collect(ctx)
    assert store.latest_scalar("search.corr2_ok")["value"] == 1
    assert len(store.events(kind="search_corr2_state")) == 1


def test_backfill_is_bounded_and_time_filtered(settings, store, ctx) -> None:
    """First start: only the last 24 h of rows, from a bounded byte window."""
    obs_recent = "2026-09-08-00:00:00"
    start = utc_start_to_unix(obs_recent)
    now = time.time()
    old_samp = int((now - start - 3 * 86400.0) / TSAMP_S)
    new_samp = int((now - start - 600.0) / TSAMP_S)
    write_file(
        settings,
        obs_recent,
        0,
        HEADER + row(16.0, old_samp, 3, 4, 50.0, 1) + row(17.0, new_samp, 3, 4, 50.0, 2),
    )
    collector = SearchCollector(
        settings,
        config=SearchConfig(
            cands_dir=settings.hella_cands_dir,
            t2_db=settings.t2_db,
            backfill_hours=24.0,
            retention_interval_s=1e9,
        ),
        ssh_runner=fake_ssh({}),
    )
    collector.collect(ctx)
    assert [float(r["snr"]) for r in store.query("SELECT snr FROM cands")] == [17.0]


def test_gulp_stats_mirror(settings, store, ctx) -> None:
    conn = sqlite3.connect(settings.t2_db)
    conn.execute(
        "CREATE TABLE gulp_stats (id INTEGER PRIMARY KEY, obs_utc_start TEXT NOT NULL, "
        "gulp INTEGER, gulp_utc TEXT NOT NULL, n_jobs INTEGER, n_cands INTEGER, "
        "n_clusters INTEGER, n_stored INTEGER, n_would INTEGER, clustering_ms REAL, "
        "created_utc TEXT, n_vetoed INTEGER DEFAULT 0, n_shed INTEGER DEFAULT 0)"
    )
    conn.executemany(
        "INSERT INTO gulp_stats (obs_utc_start, gulp, gulp_utc, n_jobs, n_cands, n_clusters, "
        "n_stored, n_would, clustering_ms, created_utc, n_vetoed, n_shed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (OBS, 1, "2026-09-08T00:00:01.100+00:00", 8, 120, 9, 3, 1, 4.5, "x", 2, 0),
            (OBS, 2, "2026-09-08T00:00:09.700+00:00", 7, 40, 4, 1, 0, 3.5, "x", 1, 0),
        ],
    )
    conn.commit()
    conn.close()

    write_file(settings, OBS, 0, HEADER)
    collector = make_collector(settings, fake_ssh({}))
    collector.collect(ctx)
    mirrored = store.query("SELECT gulp_utc, ts_unix, n_cands, n_jobs FROM gulp_stats_mirror ORDER BY ts_unix")
    assert [int(r["n_cands"]) for r in mirrored] == [120, 40]
    assert mirrored[0]["ts_unix"] == pytest.approx(parse_iso("2026-09-08T00:00:01.100+00:00"))
    # n_jobs of the newest gulp -> the 512-beam saturation indicator.
    assert store.latest_scalar("search.beams_searched_est")["value"] == 7 * 64
    assert store.get_watermark("search", "gulp_stats_utc") == "2026-09-08T00:00:09.700+00:00"

    # Re-running mirrors nothing twice (watermark + ON CONFLICT DO NOTHING).
    collector.collect(ctx)
    assert store.query("SELECT COUNT(*) AS n FROM gulp_stats_mirror")[0]["n"] == 2


def test_read_gulp_stats_missing_db_is_empty(settings) -> None:
    assert read_gulp_stats(settings.t2_db, None) == []


def test_rate_scalars(settings, store, ctx) -> None:
    obs_recent = "2026-09-08-00:00:00"
    start = utc_start_to_unix(obs_recent)
    samp = int((time.time() - start - 60.0) / TSAMP_S)
    write_file(settings, obs_recent, 0, HEADER + "".join(row(16.0, samp + i, 3, 4, 50.0, 1) for i in range(5)))
    collector = SearchCollector(
        settings,
        config=SearchConfig(
            cands_dir=settings.hella_cands_dir,
            t2_db=settings.t2_db,
            rate_window_s=300.0,
            retention_interval_s=1e9,
        ),
        ssh_runner=fake_ssh({}),
    )
    collector.collect(ctx)
    assert store.latest_scalar("search.rate_per_min.0")["value"] == pytest.approx(1.0)
    assert store.latest_scalar("search.total_rate")["value"] == pytest.approx(1.0)


def test_retention_drops_old_raw_rows_but_keeps_bins(settings, store, ctx) -> None:
    write_file(settings, OBS, 0, HEADER + row(16.0, 100, 3, 4, 50.0, 1))
    collector = SearchCollector(
        settings,
        config=SearchConfig(
            cands_dir=settings.hella_cands_dir,
            t2_db=settings.t2_db,
            backfill_hours=0.0,
            raw_ttl_days=1.0,
            retention_interval_s=0.0,
        ),
        ssh_runner=fake_ssh({}),
    )
    collector.collect(ctx)  # OBS is 4 days old, past this 1-day TTL
    assert store.query("SELECT COUNT(*) AS n FROM cands")[0]["n"] == 0
    assert store.query("SELECT COUNT(*) AS n FROM cand_bins")[0]["n"] == 1


# -- API -------------------------------------------------------------------
def seed_api_store(settings: Settings) -> None:
    store = Store(settings.db_path, store_root=settings.store_root)
    search_mod.ensure_tables(store)
    now = time.time()
    rows = []
    for i in range(200):
        job = i % 8
        rows.append(
            (
                now - 1800.0 + i * 5.0,
                job,
                "corr1" if job < 4 else "corr2",
                15.0 + (i % 20),
                i % 7,
                20.0 + 5.0 * (i % 40),
                i % 50,
                64 * job + (i % 64),
                8192 * i,
            )
        )
    store.executemany(
        "INSERT INTO cands (ts_unix, job, node, snr, width, dm, dm_idx, beam, samp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    store.executemany(
        "INSERT INTO gulp_stats_mirror (gulp_utc, ts_unix, obs_utc_start, gulp, n_jobs, n_cands, "
        "n_clusters, n_stored, n_would, n_vetoed, n_shed, clustering_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (f"g{i}", now - 1800.0 + i * 60.0, OBS, i, 8, 100, 10, 2, 1, 3, 0, 4.0)
            for i in range(30)
        ],
    )
    store.put_scalar("hella.corr1.snr", 15.0)
    store.put_scalar("hella.corr1.dm_min", 20.0)
    store.put_scalar("hella.corr2.snr", "13,15")
    store.put_scalar("hella.corr2.dm_min", 20.0)
    store.close()


@pytest.fixture
def client(settings: Settings) -> TestClient:
    seed_api_store(settings)
    reader = Store(settings.db_path, read_only=True, store_root=settings.store_root)
    app = FastAPI()
    app.include_router(build_router(reader, settings))
    with TestClient(app) as test_client:
        yield test_client
    reader.close()


def test_summary_shape(client: TestClient) -> None:
    body = client.get("/api/search/summary").json()
    assert set(body) == {"t0", "t1", "n_cands", "per_job", "thresholds", "funnel"}
    assert body["n_cands"] == 200
    assert len(body["per_job"]) == 8
    first = body["per_job"][0]
    # last_ts is unix seconds (docs/api-search.md); last_ts_iso is the extra.
    assert set(first) == {"job", "node", "n", "rate_per_min", "last_ts", "last_ts_iso"}
    assert isinstance(first["last_ts"], float)
    assert first["last_ts_iso"].endswith("Z")
    assert first["node"] == "corr1" and body["per_job"][4]["node"] == "corr2"
    assert first["n"] == 25
    assert body["thresholds"]["corr1"] == {"snr": 15.0, "dm_min": 20.0}
    assert body["thresholds"]["corr2"]["snr"] == "13,15"
    funnel = body["funnel"]
    assert funnel["n_cands"] == 3000 and funnel["n_clusters"] == 300
    assert funnel["n_stored"] == 60 and funnel["n_vetoed"] == 90
    assert funnel["n_triggers"] is None  # no t2 sqlite in the tmp store


def test_hist_fields_and_bad_params(client: TestClient) -> None:
    body = client.get("/api/search/hist", params={"field": "snr", "bins": 10}).json()
    assert len(body["edges"]) == 11 and len(body["counts"]) == 10
    assert sum(body["counts"]) == body["n_used"] == 200
    logged = client.get("/api/search/hist", params={"field": "dm", "bins": 8, "log": 1}).json()
    assert len(logged["counts"]) == 8 and logged["log"] is True
    assert logged["edges"][0] > 0  # log axis never starts at zero
    width = client.get("/api/search/hist", params={"field": "width"}).json()
    assert width["edges"] == [float(i) for i in range(10)]
    beam = client.get("/api/search/hist", params={"field": "beam", "bins": 512}).json()
    assert len(beam["counts"]) == 512
    assert client.get("/api/search/hist", params={"field": "nope"}).status_code == 400
    assert client.get("/api/search/hist", params={"bins": 0}).status_code == 422
    assert client.get("/api/search/hist", params={"t0": "not-a-time"}).status_code == 400


def test_scatter_subsamples(client: TestClient) -> None:
    body = client.get("/api/search/scatter", params={"x": "dm", "y": "snr"}).json()
    assert body["n_total"] == 200 and len(body["x"]) == len(body["y"]) == 200
    capped = client.get(
        "/api/search/scatter", params={"x": "time", "y": "beam", "max_points": 20}
    ).json()
    assert capped["n_total"] == 200 and len(capped["x"]) == 20
    assert capped["x"][0] < capped["x"][-1]  # still time-ordered
    assert client.get("/api/search/scatter", params={"x": "nope"}).status_code == 400


def test_beam_map(client: TestClient) -> None:
    body = client.get("/api/search/beam-map").json()
    assert len(body["counts"]) == 512
    assert sum(body["counts"]) == 200


def test_rate_and_funnel(client: TestClient) -> None:
    body = client.get("/api/search/rate", params={"step_s": 300}).json()
    assert set(body["per_job"]) == {str(j) for j in range(8)}
    assert len(body["t"]) == len(body["total"]) == len(body["per_job"]["0"])
    assert sum(body["total"]) > 0
    funnel = client.get("/api/search/funnel", params={"step_s": 600}).json()
    assert len(funnel["t"]) == len(funnel["n_cands"]) == len(funnel["n_vetoed"])
    assert sum(funnel["n_cands"]) == 3000
    assert client.get("/api/search/rate", params={"step_s": 0}).status_code == 422


def test_empty_store_answers_empty(settings, tmp_path) -> None:
    """A store the collector never touched renders, it does not 500."""
    fresh = Settings(store_root=tmp_path / "fresh", t2_db=tmp_path / "none.sqlite")
    Store(fresh.db_path, store_root=fresh.store_root).close()
    reader = Store(fresh.db_path, read_only=True, store_root=fresh.store_root)
    app = FastAPI()
    app.include_router(build_router(reader, fresh))
    with TestClient(app) as test_client:
        assert test_client.get("/api/search/summary").json()["n_cands"] == 0
        assert test_client.get("/api/search/hist").json()["counts"]
        assert test_client.get("/api/search/scatter").json() == {
            "x": [], "y": [], "n_total": 0, "x_field": "dm", "y_field": "snr"
        }
        assert sum(test_client.get("/api/search/beam-map").json()["counts"]) == 0
    reader.close()


# -- clamps ----------------------------------------------------------------
def test_window_clamps_span_and_rejects_inverted() -> None:
    now = 1_800_000_000.0
    start, end = window("0", str(now), now=now)
    assert end - start == pytest.approx(30 * 86400.0)
    start, end = window(None, None, now=now)
    assert end - start == pytest.approx(3600.0)
    with pytest.raises(Exception):
        window(str(now), str(now - 10), now=now)


def test_step_and_bins_raises_step_instead_of_returning_too_many() -> None:
    step, n = step_and_bins(0.0, 30 * 86400.0, 60.0)
    assert n == 5000 and step > 60.0
    assert step_and_bins(0.0, 3600.0, 60.0) == (60.0, 61)


def test_field_edges_handles_degenerate_data() -> None:
    edges = field_edges("snr", [17.0, 17.0], 10, False)
    assert len(edges) == 11 and edges[0] == 17.0 and edges[-1] > 17.0
    assert field_edges("snr", [], 10, False) == list(SNR_EDGES)
    assert field_edges("dm", [], 10, True) == list(DM_EDGES)


def test_json_histograms_are_summable_across_gulps(settings, store, ctx) -> None:
    """The stored per-gulp histograms use the fixed edges, so they add up."""
    write_file(
        settings,
        OBS,
        0,
        HEADER
        + row(16.0, 10, 3, 4, 50.0, 1)
        + row(60.0, 9000, 5, 20, 800.0, 2)
        + row(90.0, 20000, 6, 30, 2000.0, 3),
    )
    collector = make_collector(settings, fake_ssh({}))
    collector.collect(ctx)
    rows = store.query("SELECT snr_hist, dm_hist, n FROM cand_bins")
    assert len(rows) == 3
    total = [0] * (len(SNR_EDGES) - 1)
    for r in rows:
        for i, v in enumerate(json.loads(r["snr_hist"])):
            total[i] += v
    assert sum(total) == 3
    assert sum(int(r["n"]) for r in rows) == 3
