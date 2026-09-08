"""Collector runner isolation plus the pure parsers of each collector."""

from __future__ import annotations

import asyncio
import dataclasses
import time

import pytest

from casm_monitor.collectors import CollectorContext, HellaCollector
from casm_monitor.collectors.base import Collector
from casm_monitor.collectors.hella import parse_hella_cfg, parse_multi
from casm_monitor.collectors.nodes import DisksCollector, StoreCollector
from casm_monitor.collectors.obs import parse_daemon_status, parse_ps
from casm_monitor.collectors.runner import CollectorRunner, default_collectors
from casm_monitor.collectors.services import ZapdosCollector
from casm_monitor.collectors.weights import ledger_weights_basename, read_last_ledger_row
from casm_monitor.store import ShardWriter, Store

HELLA_CFG = """OUTPUT BOTH
OUTPUTPATH /mnt/nvme4/data/casm/hella_cands/cands_2026-09-04-16:42:39.dat.0
HOST 10.70.0.11
PORT 12345
DM_MIN 20
DM_MAX 1000
WIDTH_MIN 1
WIDTH_MAX 128
SNR 15
OUTPUT_BANDPASS 0
NBEAM 64
GPU 2
"""

LMC_REPLY = (
    "<lmc_reply><daemons_state>running</daemons_state>"
    "<area name='control' id='-1'><daemon type='server' name='casm_tcs'>True</daemon></area>"
    "<area name='antenna' id='0'><daemon type='stream' name='casm_ant_recv'>True</daemon>"
    "<daemon type='stream' name='casm_bfcorr'>False</daemon></area></lmc_reply>"
)

# Real corr1 command lines (2026-09-08): every GPU binary is preceded by a
# /bin/sh numactl wrapper, and medusa also runs python daemons of the same name.
PS_OUTPUT = """/bin/sh -c numactl -C 47 --membind=0 -- casm_corr_dump -b 32 -f 3072 -i 128 -k b000 -o /mnt/nvme4/data/casm/visibilities_64ant//2026-09-04-16:43:47.dat -n 32
casm_corr_dump -b 32 -f 3072 -i 128 -k b000 -o /mnt/nvme4/data/casm/visibilities_64ant//2026-09-04-16:43:47.dat -n 32
/bin/sh -c numactl -C 3,4 --membind=0 -- casm_bfcorr -a 64 -f 512 -i a000 --sub_incoh -m bf --bf_out a00c --bf_scale_factor 8
casm_bfcorr -a 64 -f 512 -i a000 -m corr --sub_incoh -t 2048 -d 0 -m bf --bf_out a00c --bf_weights a024 --bf_scale_factor 8
casm_bfcorr -a 64 -f 512 -i a002 -m corr --sub_incoh -t 2048 -d 1 -m bf --bf_out a00e --bf_weights a026 --bf_scale_factor 8
python /home/casm/software/fourier-space/opt/casm/bin/casm_bfcorr.py 0 bf_proc
/bin/sh -c numactl -C 21,22 --membind=0 -- /home/casm/software/vishnu/casm_hella_vishnu/deploy/hella_fork_wrapper.sh -c /tmp/hella_0.cfg
/home/casm/software/vishnu/casm_hella_vishnu/build/src/apps/casm_hella_refactored -c /tmp/hella_0.cfg
/home/casm/software/vishnu/casm_hella_vishnu/build/src/apps/casm_hella_refactored -c /tmp/hella_1.cfg
python /home/casm/software/fourier-space/opt/casm/bin/casm_hella.py 2 bf_proc
"""


# -- parsers ------------------------------------------------------------
def test_parse_hella_cfg():
    cfg = parse_hella_cfg(HELLA_CFG)
    assert cfg["SNR"] == "15" and cfg["DM_MIN"] == "20"
    assert cfg["OUTPUTPATH"].endswith(".dat.0")


def test_parse_multi_uses_output_path_job_index():
    text = HELLA_CFG + HELLA_CFG.replace(".dat.0", ".dat.3").replace("SNR 15", "SNR 12")
    blocks = parse_multi(text)
    assert sorted(blocks) == [0, 3]
    assert blocks[0]["SNR"] == "15" and blocks[3]["SNR"] == "12"


def test_parse_daemon_status():
    status = parse_daemon_status(LMC_REPLY)
    assert status["daemons_state"] == "running"
    assert status["up"] == 2 and status["down"] == 1
    assert status["daemons"]["antenna[0].casm_bfcorr"] is False


def test_parse_ps():
    info = parse_ps(PS_OUTPUT)
    assert info["utc_start"] == "2026-09-04-16:43:47"
    assert info["sub_incoh"] == 1  # all searching bfcorr processes carry the flag
    assert info["n_bfcorr"] == 2 and info["n_sub_incoh"] == 2
    assert info["bf_scale_factor"] == 8.0
    assert info["n_hella"] == 2  # the two search binaries, not wrappers or .py
    assert info["n_corr_dump"] == 1


def test_parse_ps_partial_sub_incoh():
    text = PS_OUTPUT.replace(
        "casm_bfcorr -a 64 -f 512 -i a002 -m corr --sub_incoh",
        "casm_bfcorr -a 64 -f 512 -i a002 -m corr",
    )
    info = parse_ps(text)
    assert info["n_bfcorr"] == 2 and info["n_sub_incoh"] == 1
    assert info["sub_incoh"] == 0


def test_ledger_row_parsing(tmp_path):
    csv_path = tmp_path / "deployed_weights.csv"
    csv_path.write_text(
        "date_deployed,weights_file,cal_file,n_ant_set,n_ant_live,threshold,amplitude,notes\n"
        "2026-08-31 23:39:32 UTC,/a/w_aug31.h5 (+ ib_aug31.h5),/a/cal_aug30.h5,17,17,1,uniform,\"multi\nline note\"\n"
        "2026-09-04 08:42:33 UTC,/b/w_sep04.h5 (+ ib_sep04.h5),/b/cal_sep03.h5,17,17,1,uniform,\"note\"\n"
    )
    row = read_last_ledger_row(csv_path)
    assert row["date_deployed"].startswith("2026-09-04")
    assert ledger_weights_basename(row) == "w_sep04.h5"
    assert read_last_ledger_row(tmp_path / "missing.csv") is None


# -- runner isolation ---------------------------------------------------
class GoodCollector(Collector):
    name = "good"

    def __init__(self, settings):
        super().__init__(settings, cadence_s=0.01)
        self.calls = 0

    def collect(self, ctx: CollectorContext) -> None:
        self.calls += 1
        ctx.scalar("good.value", float(self.calls))


class BadCollector(Collector):
    name = "bad"

    def __init__(self, settings):
        super().__init__(settings, cadence_s=0.01)

    def collect(self, ctx: CollectorContext) -> None:
        raise RuntimeError("collector exploded")


class SlowCollector(Collector):
    name = "slow"
    timeout_s = 0.05

    def __init__(self, settings):
        super().__init__(settings, cadence_s=0.01)

    def collect(self, ctx: CollectorContext) -> None:
        time.sleep(1.0)


def test_failing_collector_does_not_stop_others(settings, store: Store):
    good, bad, slow = GoodCollector(settings), BadCollector(settings), SlowCollector(settings)
    runner = CollectorRunner(settings, [good, bad, slow], store=store)

    async def three_passes():
        for _ in range(3):
            for collector in runner.collectors:
                await runner.run_once(collector)

    asyncio.run(three_passes())

    assert good.calls == 3
    assert store.latest_scalar("good.value")["value"] == 3.0
    hb = store.heartbeats()
    assert hb["good"]["last_ok"] is not None
    assert "collector exploded" in hb["bad"]["last_err"]
    assert "timeout" in hb["slow"]["last_err"]
    # a collector_ok scalar per collector, 0 for the broken ones
    oks = {
        r["tags"]: r["value"]
        for r in store.query("SELECT tags, value FROM scalars WHERE name = 'collector_ok'")
    }
    assert '{"collector": "good"}' in oks and oks['{"collector": "good"}'] == 1
    assert oks['{"collector": "bad"}'] == 0
    # three consecutive failures -> exactly one error event per broken collector
    failing = store.events(kind="collector_failing")
    assert {e["subject"] for e in failing} == {"bad", "slow"}
    assert all(e["severity"] == "error" for e in failing)
    assert len(failing) == 2


def test_on_change_emits_only_on_change(settings, store: Store):
    ctx = CollectorContext(settings=settings, store=store, shards=ShardWriter(store))
    assert ctx.on_change("obs.utc_start", "A", kind="obs_restart") is False  # first sighting
    assert ctx.on_change("obs.utc_start", "A", kind="obs_restart") is False
    assert ctx.on_change("obs.utc_start", "B", kind="obs_restart") is True
    events = store.events(kind="obs_restart")
    assert len(events) == 1
    assert events[0]["detail"]["from"] == "A" and events[0]["detail"]["to"] == "B"


def test_default_collector_set_names(settings):
    names = [c.name for c in default_collectors(settings)]
    assert names == [
        "obs",
        "hella",
        "hella_corr2",
        "services",
        "zapdos",
        "disks",
        "gpus",
        "weights",
        "sky",
        "store",
    ]


def test_zapdos_probe_is_rate_limited(settings, store: Store, monkeypatch):
    """The hourly guard is persisted, so no restart loop can hammer zapdos."""
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=10.0):
        calls.append(cmd)
        return 0, "", ""

    monkeypatch.setattr("casm_monitor.collectors.services.run", fake_run)
    ctx = CollectorContext(settings=settings, store=store, shards=ShardWriter(store))
    collector = ZapdosCollector(settings)
    collector.collect(ctx)
    collector.collect(ctx)
    collector.collect(ctx)
    assert len(calls) == 1
    assert calls[0][:3] == ["ssh", "-o", "BatchMode=yes"]
    assert store.latest_scalar("services.zapdos_ok")["value"] == 1

    # only after the interval has passed does a second probe happen
    store.set_watermark("zapdos", "last_probe_ts", time.time() - 2 * settings.zapdos_min_interval_s)
    collector.collect(ctx)
    assert len(calls) == 2


def test_disks_and_store_collectors_run_locally(settings, store: Store, tmp_path):
    ctx = CollectorContext(settings=settings, store=store, shards=ShardWriter(store))
    local = dataclasses.replace(settings, disks=(str(tmp_path),))
    DisksCollector(local).collect(
        CollectorContext(settings=local, store=store, shards=ShardWriter(store))
    )
    key = str(tmp_path).strip("/").replace("/", "_")
    assert store.latest_scalar(f"disk.{key}.pct_used")["value"] >= 0.0

    StoreCollector(settings).collect(ctx)
    assert store.latest_scalar("store.sqlite_bytes")["value"] > 0
    assert store.latest_scalar("store.shards")["value"] == 0


def test_hella_collector_reads_local_cfgs(settings, store: Store, tmp_path, monkeypatch):
    collector = HellaCollector(settings, node="corr1")
    monkeypatch.setattr(collector, "_read", lambda: parse_multi(HELLA_CFG))
    ctx = CollectorContext(settings=settings, store=store, shards=ShardWriter(store))
    collector.collect(ctx)
    assert store.latest_scalar("hella.corr1.snr")["value"] == 15.0
    assert store.latest_scalar("hella.corr1.dm_min")["value"] == 20.0
    assert store.latest_scalar("hella.corr1.n_jobs")["value"] == 1


def test_hella_corr2_failure_is_tolerated(settings, store: Store, monkeypatch):
    collector = HellaCollector(settings, node="corr2")
    def boom():
        raise RuntimeError("ssh casm-corr2 failed")
    monkeypatch.setattr(collector, "_read", boom)
    ctx = CollectorContext(settings=settings, store=store, shards=ShardWriter(store))
    collector.collect(ctx)  # must not raise
    assert store.latest_scalar("hella.corr2.n_jobs")["value"] == 0
    assert store.events(kind="hella_cfg_unavailable")
