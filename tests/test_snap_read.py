"""Board-read tests. Nothing here contacts zapdos or any SNAP board.

The remote script is exercised against a fake ``SnapFengine`` (it is loaded
from its path, not imported as a module, because it is python-3.8 code meant
for another host), so the npz key layout is tested by the same code that
writes it on zapdos.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from casm_monitor.collectors.base import CollectorContext
from casm_monitor.collectors.snapread import SnapReadCollector, snap_read_interval_s, SCHEDULE_KEY
from casm_monitor.config import Settings
from casm_monitor.jobs.kinds import KINDS
from casm_monitor.jobs.snap_read import (
    LAST_MANUAL_KEY,
    LOCK_STREAM,
    LeaseRenewer,
    acquire_lock,
    eq_epoch,
    ingest,
    latest_reads,
    lock_holder,
    parse_npz,
    pps_summary,
    release_lock,
    renew_lock,
    ssh_command,
)
from casm_monitor.store import ShardWriter, Store
from casm_monitor.web.snapread import build_router, manual_refusal

REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_SCRIPT = REPO_ROOT / "casm_monitor" / "remote" / "snap_read_remote.py"
IP_A = "192.168.120.52"
IP_RELAY = "192.168.120.59"
FS_HZ = 250_000_000


# -- fixtures -----------------------------------------------------------
@pytest.fixture
def snap_settings(tmp_path: Path) -> Settings:
    snap_map = tmp_path / "casm_snap_map.csv"
    snap_map.write_text(
        "chassis,slot,feng_id,snap_ip\n"
        f"1,A,0,{IP_A}\n"
        "1,I,1,192.168.120.51\n"
    )
    layout = tmp_path / "layout.csv"
    layout.write_text(
        "antenna,x,y,z,snap,adc,packet_idx,functional,row,col\n"
        "1,-0.1,0,0,0,8,8,1,N21,E1\n"
        "2,0.35,0,0,0,9,9,1,N21,E2\n"
    )
    return Settings(
        store_root=tmp_path / "store",
        snap_map_csv=snap_map,
        snap_layout_csv=layout,
        snap_relay_boards=(IP_RELAY,),
        snap_manual_min_interval_s=300.0,
    )


@pytest.fixture
def snap_store(snap_settings: Settings) -> Store:
    st = Store(snap_settings.db_path, store_root=snap_settings.store_root)
    yield st
    st.close()


def _load_remote_module() -> Any:
    spec = importlib.util.spec_from_file_location("snap_read_remote_under_test", REMOTE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeSnap:
    """A board that answers every read-only getter and nothing else."""

    n_signals_per_block = 2
    fs_hz = float(FS_HZ)

    def __init__(self, programmed: bool = True, eq_scale: float = 1.0, acc_len: int = 1024) -> None:
        self.programmed = programmed
        self.acc_len = acc_len
        self.eq_scale = eq_scale
        self.fpga = self
        self._cfpga = self
        self.autocorr = self
        self.input = self
        self.eq = self
        self.pfb = self
        self.sync = self
        self.packetizer = self
        self.n_total_blocks = 4

    # fpga
    def is_programmed(self) -> bool:
        return self.programmed

    def listdev(self):
        return ['version_version'] if self.programmed else ['sys_clkcounter']

    # autocorr
    def get_acc_len(self) -> int:
        return self.acc_len

    def get_new_spectra(self, signal_block: int = 0) -> np.ndarray:
        base = np.arange(4096, dtype=np.float32)
        return np.stack([base + 2 * signal_block, base + 2 * signal_block + 1])

    # input
    def get_status(self) -> tuple[dict[str, Any], dict[str, Any]]:
        stats: dict[str, Any] = {}
        for i in range(12):
            stats["rms%02d" % i] = 10.0 + i
            stats["mean%02d" % i] = 0.1 * i
            stats["power%02d" % i] = 100.0 + i
            stats["switch_position%02d" % i] = "adc"
        return stats, {}

    # eq
    def get_coeffs(self, inputid: int) -> np.ndarray:
        return np.full(512, self.eq_scale * (inputid + 1), dtype=float)

    # pfb
    def get_fft_shift(self) -> int:
        return 0x1FFF

    def get_overflow_count(self) -> int:
        return 3

    # sync
    def count_pps(self) -> int:
        return 12345

    def count_ext(self) -> int:
        return 6789

    def period_pps(self) -> int:
        return FS_HZ

    def period(self) -> int:
        return FS_HZ

    def read_uint(self, name: str) -> int:
        assert name == "ctrl"
        return 1 << 11

    # packetizer
    def read(self, name: str, n: int) -> bytes:
        if name == "ant_chan":
            return np.array([0 << 16, 0 << 16, 0 << 16, 0 << 16], dtype=">u4").tobytes()
        return np.array([1, 1, 1, 0], dtype=">u1").tobytes()


def make_npz(
    ips=(IP_A,),
    *,
    eq_scale: float = 1.0,
    programmed: bool = True,
    budget_s: float = 60.0,
    acc_len: int = 1024,
    connect: Any = None,
) -> bytes:
    """Run the real remote script against fake boards; return its stdout."""
    module = _load_remote_module()
    module.BoardReader._connect = connect or (
        lambda self: FakeSnap(programmed, eq_scale, acc_len)
    )

    # The script writes the archive to the ORIGINAL fd 1 (it redirects
    # sys.stdout/fd 1 to stderr for the run so no library print can corrupt
    # the npz), so the capture has to be at the descriptor level.
    with tempfile.TemporaryFile() as sink:
        saved = os.dup(1)
        os.dup2(sink.fileno(), 1)
        try:
            rc = module.main([f"--budget={budget_s:g}", *ips])
        finally:
            os.dup2(saved, 1)
            os.close(saved)
        assert rc == 0
        sink.seek(0)
        return sink.read()


# -- 1. npz parsing -----------------------------------------------------
def test_parse_npz_from_synthetic_read() -> None:
    parsed = parse_npz(make_npz((IP_A, IP_RELAY)))
    assert set(parsed["boards"]) == {IP_A, IP_RELAY}
    board = parsed["boards"][IP_A]
    assert board["spectra"].shape == (12, 4096)
    assert board["eq_coeffs"].shape == (12, 512)
    assert board["adc_rms"].shape == (12,)
    assert board["programmed"] is True
    assert board["feng_ids_hw"] == [0]
    assert board["pps"]["count_pps"] == 12345
    assert board["pps"]["loopback"] is True
    assert board["errors"] == {}
    assert board["switch_position"] == ["adc"] * 12
    # ADC input 5 comes from signal block 2, second signal: base + 5.
    assert board["spectra"][5][10] == pytest.approx(15.0)


def test_parse_npz_rejects_garbage() -> None:
    buf = io.BytesIO()
    np.savez_compressed(buf, something=np.zeros(3))
    with pytest.raises(ValueError):
        parse_npz(buf.getvalue())


def test_remote_failed_inventory_is_unknown_not_unprogrammed():
    class Unreachable(FakeSnap):
        def listdev(self):
            raise RuntimeError('Access violation')

        def get_new_spectra(self, signal_block=0):
            raise AssertionError('Unavailable control must skip acquisition')

    parsed = parse_npz(make_npz((IP_A, IP_RELAY), connect=lambda b: Unreachable() if b.ip == IP_A else FakeSnap()))
    board = parsed['boards'][IP_A]
    assert board['programmed'] is None
    assert 'Access violation' in board['errors']['firmware_register_map']
    assert 'firmware state unknown' in board['errors']['autocorr']
    assert np.isnan(board['spectra']).all()
    assert parsed['boards'][IP_RELAY]['programmed'] is True


def test_remote_unprogrammed_board_reports_pps_only() -> None:
    parsed = parse_npz(make_npz((IP_RELAY,), programmed=False))
    board = parsed["boards"][IP_RELAY]
    assert board["programmed"] is False
    assert np.all(np.isnan(board["spectra"]))
    # ...and it says why there is no data, which is what makes the archive
    # acceptable rather than a "partial read" failure.
    assert board["errors"] == {"autocorr": "skipped: programmed=False"}
    assert board["pps"]["count_pps"] == 12345  # sync is still attempted


def test_remote_skips_autocorr_when_not_accumulating() -> None:
    """acc_len 0 (the relay boards): reading spectra would burn ~45 s of budget
    in _wait_for_acc and return infinities, so it is skipped explicitly."""
    parsed = parse_npz(make_npz((IP_RELAY,), acc_len=0))
    board = parsed["boards"][IP_RELAY]
    assert board["errors"] == {"autocorr": "skipped: acc_len=0"}
    assert np.all(np.isnan(board["spectra"]))
    assert board["adc_rms"][0] == pytest.approx(10.0)  # the cheap reads still ran
    assert board["elapsed_s"] < 5.0


def test_remote_budget_zero_skips_calls() -> None:
    parsed = parse_npz(make_npz((IP_A,), budget_s=0.0))
    board = parsed["boards"][IP_A]
    assert board["errors"]["connect"] == "budget_exceeded"


class HangingSnap(FakeSnap):
    """A board that answers its socket but never returns from one getter."""

    def get_new_spectra(self, signal_block: int = 0) -> np.ndarray:
        time.sleep(30.0)
        raise AssertionError("the alarm should have interrupted this")


def test_remote_hard_timeout_abandons_one_board_and_reads_the_next() -> None:
    """A hung KATCP call is interrupted by the per-call alarm, that board is
    abandoned, and the next board still gets its own full budget -- one hang
    cannot eat the whole ssh session."""
    started = time.time()
    data = make_npz(
        (IP_A, IP_RELAY),
        budget_s=1.0,
        connect=lambda self: HangingSnap() if self.ip == IP_A else FakeSnap(),
    )
    elapsed = time.time() - started
    parsed = parse_npz(data, requested_ips=(IP_A, IP_RELAY))
    hung = parsed["boards"][IP_A]
    assert "timeout after 1s" in hung["errors"]["autocorr0"]
    # Everything after the hang on that board is skipped, not attempted.
    assert hung["errors"]["autocorr1"] == "board_aborted"
    assert np.all(np.isnan(hung["spectra"]))
    # The second board was read normally.
    good = parsed["boards"][IP_RELAY]
    assert good["errors"] == {}
    assert good["spectra"][0][10] == pytest.approx(10.0)
    # Two boards, one of them hung for 30 s of sleep: well under that.
    assert elapsed < 15.0


class ChattySnap(FakeSnap):
    """A board whose library prints to stdout in the middle of the read."""

    def get_status(self) -> tuple[dict[str, Any], dict[str, Any]]:
        print("casperfpga: chatter that must not reach the archive")
        sys.stdout.write("more chatter\n")
        sys.stdout.flush()
        return super().get_status()


def test_remote_stdout_chatter_does_not_corrupt_the_archive() -> None:
    data = make_npz((IP_A,), connect=lambda self: ChattySnap())
    assert b"chatter" not in data[:64]
    parsed = parse_npz(data, requested_ips=(IP_A,))
    assert parsed["boards"][IP_A]["adc_rms"][0] == pytest.approx(10.0)


def test_parse_npz_validates_version_ips_and_arrays() -> None:
    good = make_npz((IP_A, IP_RELAY))
    assert set(parse_npz(good, requested_ips=(IP_A, IP_RELAY))["boards"]) == {IP_A, IP_RELAY}

    # A board missing from the answer is a failed read, not a missing card.
    with pytest.raises(ValueError, match="requested"):
        parse_npz(good, requested_ips=(IP_A, IP_RELAY, "192.168.120.51"))
    with pytest.raises(ValueError, match="requested"):
        parse_npz(good, requested_ips=(IP_A,))

    def repack(mutate) -> bytes:
        with np.load(io.BytesIO(good), allow_pickle=False) as npz:
            payload = {k: npz[k] for k in npz.files}
        meta = json.loads(str(payload["meta_json"][()]))
        mutate(meta, payload)
        payload["meta_json"] = np.array(json.dumps(meta))
        buf = io.BytesIO()
        np.savez_compressed(buf, **payload)
        return buf.getvalue()

    def bump_version(meta, _payload) -> None:
        meta["version"] = 99
        meta["script_version"] = 99

    with pytest.raises(ValueError, match="version"):
        parse_npz(repack(bump_version))

    def drop_spectra(_meta, payload) -> None:
        payload.pop(f"{IP_A}__spectra")

    with pytest.raises(ValueError, match="missing its spectra"):
        parse_npz(repack(drop_spectra))

    def blank_a_board(meta, payload) -> None:
        payload[f"{IP_A}__spectra"] = np.full((12, 4096), np.nan, dtype=np.float32)
        meta["boards"][IP_A]["errors"] = {}

    with pytest.raises(ValueError, match="no spectra and no error"):
        parse_npz(repack(blank_a_board))


# -- 2. the persisted lock ---------------------------------------------
def test_lock_is_compare_and_set(snap_store: Store) -> None:
    now = 1000.0
    token_a = acquire_lock(snap_store, "a", ttl_s=600.0, now=now)
    assert token_a
    assert acquire_lock(snap_store, "b", ttl_s=600.0, now=now + 1) is None
    assert lock_holder(snap_store, now=now + 1)["holder"] == "a"
    # Somebody else's token cannot release or renew a's lease; a's can.
    assert release_lock(snap_store, "not-a-token") is False
    assert renew_lock(snap_store, "not-a-token", ttl_s=600.0, now=now) is False
    assert release_lock(snap_store, token_a) is True
    assert lock_holder(snap_store, now=now + 2) is None


def test_expired_lock_is_taken_over_only_when_the_holder_stops_renewing(
    snap_store: Store,
) -> None:
    """A lease expiry means the holder is dead. While it renews, no takeover."""
    t0 = 1000.0
    token = acquire_lock(snap_store, "holder", ttl_s=600.0, now=t0)
    assert token
    assert acquire_lock(snap_store, "next", ttl_s=600.0, now=t0 + 599.0) is None

    # The holder renews every 60 s: a read that runs 20 minutes keeps the lock.
    for step in range(1, 21):
        assert renew_lock(snap_store, token, ttl_s=600.0, now=t0 + 60.0 * step) is True
        assert acquire_lock(snap_store, "next", ttl_s=600.0, now=t0 + 60.0 * step + 1.0) is None
    assert lock_holder(snap_store, now=t0 + 1200.0)["holder"] == "holder"

    # It stops renewing (crash): the lease still has to run out first.
    last_renewal = t0 + 1200.0
    assert acquire_lock(snap_store, "next", ttl_s=600.0, now=last_renewal + 599.0) is None
    taken = acquire_lock(snap_store, "next", ttl_s=600.0, now=last_renewal + 601.0)
    assert taken and taken != token
    assert lock_holder(snap_store, now=last_renewal + 602.0)["holder"] == "next"
    # The dead holder's token is now worthless: it can neither renew nor release.
    assert renew_lock(snap_store, token, now=last_renewal + 602.0) is False
    assert release_lock(snap_store, token) is False


def test_lease_renewer_kills_the_ssh_when_ownership_is_lost() -> None:
    """The renewal loop is the stop signal: losing the row kills the process
    group before the new owner can touch the boards."""
    killed: list[str] = []
    answers = [True, True, False]

    renewer = LeaseRenewer(
        lambda: answers.pop(0), lambda: killed.append("killpg"), interval_s=0.01
    )
    with renewer:
        deadline = time.time() + 5.0
        while not renewer.lost and time.time() < deadline:
            time.sleep(0.01)
    assert renewer.lost is True
    assert renewer.renewals == 2
    assert killed == ["killpg"]


def test_lease_renewer_that_keeps_ownership_kills_nothing() -> None:
    killed: list[str] = []
    renewer = LeaseRenewer(lambda: True, lambda: killed.append("killpg"), interval_s=0.01)
    with renewer:
        time.sleep(0.1)
    assert renewer.lost is False
    assert renewer.renewals >= 2
    assert killed == []


# -- 3. ingest, eq_epoch and events ------------------------------------
def _ingest(store: Store, settings: Settings, eq_scale: float, **kw: Any) -> dict[str, Any]:
    return ingest(
        store,
        settings,
        parse_npz(make_npz((IP_A,), eq_scale=eq_scale)),
        shards=ShardWriter(store, settings.shards_root),
        **kw,
    )


def test_ingest_writes_shard_scalars_and_summary(
    snap_store: Store, snap_settings: Settings
) -> None:
    out = _ingest(snap_store, snap_settings, 1.0)
    assert out[IP_A]["shard_id"] is not None
    shards = snap_store.list_shards("snap_read")
    assert len(shards) == 1 and shards[0]["shape"] == [12, 4096]

    rms = snap_store.latest_scalar(f"snap.{IP_A}.adc_rms")
    assert rms is not None and rms["value"] == pytest.approx(21.0)  # adc 11
    # adc 8 is antenna 1 / packet_idx 8 in the layout fixture.
    tagged = [
        json.loads(r["tags"])
        for r in snap_store.query(
            "SELECT tags FROM scalars WHERE name = ?", (f"snap.{IP_A}.adc_rms",)
        )
    ]
    assert {"adc": 8, "packet_idx": 8, "antenna": 1, "mapping": "layout", "ip": IP_A} in tagged
    assert snap_store.latest_scalar(f"snap.{IP_A}.band_power_db") is not None

    summary = latest_reads(snap_store)[IP_A]
    assert summary["feng_id_cfg"] == 0 and summary["feng_id_hw"] == 0
    assert summary["adc_gain"] is None
    assert summary["pps"]["ok"] is True
    assert summary["eq_epoch"] == eq_epoch(
        np.stack([np.full(512, i + 1, dtype=np.float32) for i in range(12)]), 0x1FFF
    )


def test_eq_change_emits_one_event(snap_store: Store, snap_settings: Settings) -> None:
    first = _ingest(snap_store, snap_settings, 1.0)
    same = _ingest(snap_store, snap_settings, 1.0)
    assert snap_store.events(kind="eq_changed") == []
    changed = _ingest(snap_store, snap_settings, 2.0)
    events = snap_store.events(kind="eq_changed")
    assert len(events) == 1
    assert events[0]["detail"]["from"] == first[IP_A]["eq_epoch"] == same[IP_A]["eq_epoch"]
    assert events[0]["detail"]["to"] == changed[IP_A]["eq_epoch"]


def test_unprogrammed_and_feng_mismatch_events(
    snap_store: Store, snap_settings: Settings
) -> None:
    parsed = parse_npz(make_npz((IP_A,), programmed=False))
    parsed["boards"][IP_A]["feng_ids_hw"] = [2]
    ingest(snap_store, snap_settings, parsed, shards=ShardWriter(snap_store, snap_settings.shards_root))
    assert [e["kind"] for e in snap_store.events(kind="board_unprogrammed")] == ["board_unprogrammed"]
    mismatch = snap_store.events(kind="feng_id_mismatch")
    assert len(mismatch) == 1 and mismatch[0]["detail"]["feng_ids_hw"] == [2]


def test_pps_summary_flags_unreadable_sync() -> None:
    errors = {k: "boom" for k in ("count_pps", "count_ext", "period_pps", "period", "sync_ctrl")}
    out = pps_summary({"count_pps": None, "period_pps": None}, errors)
    assert out["ok"] is False and "sync unreadable" in out["detail"]
    # The live value measured on 2026-09-08, judged against the board's clock.
    good = pps_summary(
        {"count_pps": 1739177, "period_pps": 249998690, "count_ext": 809856}, {}, float(FS_HZ)
    )
    assert good["ok"] is True and good["period"] == 249998690
    assert "clk=250 MHz" in good["detail"]
    # 2^29 free-running ticks is not one second of that clock.
    assert pps_summary({"count_pps": 1, "period_pps": 536870912}, {}, float(FS_HZ))["ok"] is False


def test_ssh_command_is_read_only_and_pipes_the_script(snap_settings: Settings) -> None:
    cmd = ssh_command(snap_settings, [IP_A])
    assert cmd[:2] == ["ssh", "-o"] and "python3" in cmd and "-" in cmd
    assert cmd[-1] == IP_A and "--budget=60" in cmd
    text = REMOTE_SCRIPT.read_text()
    for forbidden in ("program_", "health_sweep", "set_coeffs", "arm_sync", "sw_sync", "initialize("):
        assert forbidden not in text.split('"""', 2)[2], forbidden


# -- 4. the 429 logic ---------------------------------------------------
def _client(store: Store, settings: Settings) -> TestClient:
    app = FastAPI()
    app.include_router(build_router(store, store, settings))
    return TestClient(app)


def test_post_board_read_submits_then_rate_limits(
    snap_store: Store, snap_settings: Settings
) -> None:
    client = _client(snap_store, snap_settings)
    first = client.post("/api/snaps/board-read", json={"ips": None})
    assert first.status_code == 200
    job_id = first.json()["job_id"]
    assert snap_store.get_job(job_id)["params"] == {"ips": None, "reason": "manual"}

    # Second click: refused because the job is still queued.
    second = client.post("/api/snaps/board-read", json={"ips": None})
    assert second.status_code == 429 and second.json()["retry_after_s"] > 0

    # With the job finished, the 5-minute manual limit is what refuses.
    snap_store.finish_job(job_id, "done", {"status": "ok"})
    third = client.post("/api/snaps/board-read", json={"ips": None})
    assert third.status_code == 429
    assert "one per 300" in third.json()["detail"]
    assert 0 < third.json()["retry_after_s"] <= 300

    # Older than the limit: allowed again.
    snap_store.set_watermark(LOCK_STREAM, LAST_MANUAL_KEY, time.time() - 301.0)
    assert client.post("/api/snaps/board-read", json={"ips": None}).status_code == 200


def test_post_refused_while_lock_held(snap_store: Store, snap_settings: Settings) -> None:
    acquire_lock(snap_store, "someone", ttl_s=600.0)
    client = _client(snap_store, snap_settings)
    resp = client.post("/api/snaps/board-read", json={"ips": None})
    assert resp.status_code == 429 and "already running" in resp.json()["detail"]
    assert manual_refusal(snap_store, snap_settings) is not None
    release_lock(snap_store, "someone")


def test_post_rejects_unknown_ip(snap_store: Store, snap_settings: Settings) -> None:
    client = _client(snap_store, snap_settings)
    assert client.post("/api/snaps/board-read", json={"ips": ["10.0.0.1"]}).status_code == 400
    assert client.post("/api/snaps/board-read", json={"ips": []}).status_code == 400
    assert client.post("/api/snaps/board-read", json={"ips": [7]}).status_code == 400


def test_post_deduplicates_the_requested_ips(
    snap_store: Store, snap_settings: Settings
) -> None:
    """The same board twice in one body would be read twice in one pass."""
    client = _client(snap_store, snap_settings)
    resp = client.post("/api/snaps/board-read", json={"ips": [IP_A, IP_A, IP_RELAY, IP_A]})
    assert resp.status_code == 200
    job = snap_store.get_job(resp.json()["job_id"])
    assert job["params"]["ips"] == [IP_A, IP_RELAY]


def test_two_concurrent_posts_produce_exactly_one_job(
    snap_settings: Settings,
) -> None:
    """The check-and-enqueue is one transaction, so of two clicks landing in the
    same millisecond (each with its own store handle, as two web workers would
    have) exactly one gets a job."""
    import threading

    stores = [
        Store(snap_settings.db_path, store_root=snap_settings.store_root) for _ in range(2)
    ]
    try:
        clients = [_client(st, snap_settings) for st in stores]
        start = threading.Barrier(2)
        codes: list[int] = []
        lock = threading.Lock()

        def click(client: TestClient) -> None:
            start.wait()
            resp = client.post("/api/snaps/board-read", json={"ips": None})
            with lock:
                codes.append(resp.status_code)

        threads = [threading.Thread(target=click, args=(c,)) for c in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)
        assert sorted(codes) == [200, 429]
        jobs = [j for j in stores[0].list_jobs() if j["kind"] == "snap_read"]
        assert len(jobs) == 1
    finally:
        for st in stores:
            st.close()


def test_manual_read_takes_the_hourly_zapdos_slot(
    snap_store: Store, snap_settings: Settings
) -> None:
    """A scheduler tick right after a manual read must not add a second
    contact: the manual POST claimed the shared slot itself."""
    client = _client(snap_store, snap_settings)
    assert client.post("/api/snaps/board-read", json={"ips": None}).status_code == 200
    job_id = [j for j in snap_store.list_jobs() if j["kind"] == "snap_read"][0]["id"]
    claimed = snap_store.get_watermark("zapdos", "last_probe_ts")
    assert claimed is not None

    ctx = CollectorContext(
        settings=snap_settings,
        store=snap_store,
        shards=ShardWriter(snap_store, snap_settings.shards_root),
    )
    collector = SnapReadCollector(snap_settings)
    collector.collect(ctx)
    assert len([j for j in snap_store.list_jobs() if j["kind"] == "snap_read"]) == 1

    # Even once that job is finished and the lock free, the hour is spent.
    snap_store.finish_job(job_id, "done", {"status": "ok"})
    # The actual worker records completion during ingest; finish_job alone is
    # just the synthetic queue state transition used by this fixture.
    snap_store.set_watermark(LOCK_STREAM, "last_read_ts", time.time())
    collector.collect(ctx)
    assert len([j for j in snap_store.list_jobs() if j["kind"] == "snap_read"]) == 1
    assert float(snap_store.get_watermark("zapdos", "last_probe_ts")) == float(claimed)


def test_scheduler_does_not_submit_while_the_read_lease_is_held(
    snap_store: Store, snap_settings: Settings
) -> None:
    token = acquire_lock(snap_store, "someone-else")
    assert token
    ctx = CollectorContext(
        settings=snap_settings,
        store=snap_store,
        shards=ShardWriter(snap_store, snap_settings.shards_root),
    )
    SnapReadCollector(snap_settings).collect(ctx)
    assert [j for j in snap_store.list_jobs() if j["kind"] == "snap_read"] == []
    assert snap_store.get_watermark("zapdos", "last_probe_ts") is None
    release_lock(snap_store, token)


# -- 5. GET ------------------------------------------------------------
def test_get_board_read_never_read_then_read(
    snap_store: Store, snap_settings: Settings
) -> None:
    client = _client(snap_store, snap_settings)
    empty = client.get("/api/snaps/board-read", params={"ip": IP_A}).json()
    assert empty["ts"] is None and empty["spectra"] is None
    assert empty["pps"]["detail"] == "never read"
    assert client.get("/api/snaps/board-read", params={"ip": "10.0.0.1"}).status_code == 404

    _ingest(snap_store, snap_settings, 1.0)
    body = client.get("/api/snaps/board-read", params={"ip": IP_A}).json()
    assert body["ts"].endswith("Z") and body["age_s"] >= 0
    assert len(body["freq_mhz"]) == 4096 and body["freq_mhz"][0] > body["freq_mhz"][-1]
    assert len(body["spectra"]) == 12 and len(body["spectra"][0]) == 4096
    assert body["adc_rms"][0] == pytest.approx(10.0)
    assert body["adc_gain"] is None
    assert body["feng_id_hw"] == 0 and body["feng_id_cfg"] == 0
    assert body["programmed"] is True


# -- 6. the scheduler ---------------------------------------------------
def test_scheduler_claims_at_most_once_per_hour(
    snap_store: Store, snap_settings: Settings
) -> None:
    ctx = CollectorContext(
        settings=snap_settings,
        store=snap_store,
        shards=ShardWriter(snap_store, snap_settings.shards_root),
    )
    collector = SnapReadCollector(snap_settings)
    for _ in range(20):
        collector.collect(ctx)
    jobs = [j for j in snap_store.list_jobs() if j["kind"] == "snap_read"]
    assert len(jobs) == 1
    assert jobs[0]["params"] == {"ips": None, "reason": "scheduled"}

    # The SNAP slot is independent; a submitted read also stamps liveness.
    assert snap_read_interval_s(snap_settings) == 3600.0
    last = float(snap_store.get_watermark("zapdos", "last_probe_ts"))
    assert snap_store.get_watermark(LOCK_STREAM, SCHEDULE_KEY) == last
    snap_store.set_watermark(LOCK_STREAM, SCHEDULE_KEY, last - 3601.0)
    snap_store.finish_job(jobs[0]["id"], "done", {"status": "ok"})
    collector.collect(ctx)
    assert len([j for j in snap_store.list_jobs() if j["kind"] == "snap_read"]) == 2


def test_liveness_probe_cannot_starve_due_two_hour_spectra(snap_store, snap_settings):
    from dataclasses import replace
    settings = replace(snap_settings, snap_read_interval_s=7200, zapdos_min_interval_s=7200)
    now = time.time()
    snap_store.set_watermark("zapdos", "last_probe_ts", now)
    snap_store.set_watermark(LOCK_STREAM, "last_read_ts", now - 7201)
    ctx = CollectorContext(settings=settings, store=snap_store,
                           shards=ShardWriter(snap_store, settings.shards_root))
    collector = SnapReadCollector(settings)
    collector.collect(ctx)
    jobs = [j for j in snap_store.list_jobs() if j["kind"] == "snap_read"]
    assert len(jobs) == 1
    snap_store.finish_job(jobs[0]["id"], "failed", {"error": "fixture"})
    collector.collect(ctx)
    assert len([j for j in snap_store.list_jobs() if j["kind"] == "snap_read"]) == 1


def test_recent_manual_completion_delays_scheduled_read(snap_store, snap_settings):
    snap_store.set_watermark(LOCK_STREAM, "last_read_ts", time.time())
    ctx = CollectorContext(settings=snap_settings, store=snap_store,
                           shards=ShardWriter(snap_store, snap_settings.shards_root))
    SnapReadCollector(snap_settings).collect(ctx)
    assert not [j for j in snap_store.list_jobs() if j["kind"] == "snap_read"]


def test_scheduler_does_not_double_up_on_a_manual_read(
    snap_store: Store, snap_settings: Settings
) -> None:
    snap_store.submit_job("snap_read", {"ips": None, "reason": "manual"})
    ctx = CollectorContext(
        settings=snap_settings,
        store=snap_store,
        shards=ShardWriter(snap_store, snap_settings.shards_root),
    )
    SnapReadCollector(snap_settings).collect(ctx)
    assert len([j for j in snap_store.list_jobs() if j["kind"] == "snap_read"]) == 1
    assert snap_store.latest_scalar("snap.scheduled_skipped")["value"] == 1


def test_snap_read_kind_is_registered() -> None:
    assert "snap_read" in KINDS and KINDS["snap_read"].timeout_s >= 600
