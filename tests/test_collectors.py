"""Collector runner isolation plus the pure parsers of each collector."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
import time
from pathlib import Path

import pytest

from casm_monitor.collectors import CollectorContext, HellaCollector
from casm_monitor.collectors.base import Collector
from casm_monitor.collectors.hella import parse_cfg_files, parse_hella_cfg, split_marked
from casm_monitor.collectors.nodes import DisksCollector, StoreCollector
from casm_monitor.collectors.obs import parse_daemon_status, parse_ps
from casm_monitor.collectors.runner import CollectorRunner, default_collectors
from casm_monitor.collectors.services import ZapdosCollector, zapdos_interval_s
from casm_monitor.collectors.weights import (
    WeightsCollector,
    ledger_weights_basename,
    read_last_ledger_row,
)
from casm_monitor.store import ShardWriter, Store

FIXTURES = Path(__file__).parent / "fixtures"

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


def test_parse_cfg_files_keys_on_the_filename():
    """One parse per file, job id from the filename — never a token split."""
    files = {
        "/tmp/hella_0.cfg": HELLA_CFG,
        "/tmp/hella_3.cfg": HELLA_CFG.replace(".dat.0", ".dat.3").replace("SNR 15", "SNR 12"),
    }
    blocks = parse_cfg_files(files)
    assert sorted(blocks) == [0, 3]
    assert blocks[0]["SNR"] == "15" and blocks[3]["SNR"] == "12"
    # every key of every file survives (a concatenation split on 'OUTPUT '
    # merged these into one block)
    assert blocks[3]["OUTPUT_BANDPASS"] == "0" and blocks[3]["OUTPUT"] == "BOTH"


def test_parse_live_corr1_cfg_fixtures():
    """The four live corr1 files (captured 2026-09-08): jobs 0-3, SNR 15, DM_MIN 20."""
    files = {
        str(p): p.read_text() for p in sorted((FIXTURES / "hella" / "corr1").glob("hella_*.cfg"))
    }
    blocks = parse_cfg_files(files)
    assert sorted(blocks) == [0, 1, 2, 3]
    assert {b["SNR"] for b in blocks.values()} == {"15"}
    assert {b["DM_MIN"] for b in blocks.values()} == {"20"}
    assert [blocks[j]["OUTPUTPATH"].rsplit(".", 1)[-1] for j in sorted(blocks)] == list("0123")
    assert [blocks[j]["BEAM0"] for j in sorted(blocks)] == ["0", "64", "128", "192"]


def test_parse_live_corr2_marked_fixture():
    """The corr2 ssh read (``=== <file>`` markers): jobs 4-7, same thresholds."""
    text = (FIXTURES / "hella" / "corr2_marked.txt").read_text()
    files = split_marked(text)
    assert sorted(files) == [f"/tmp/hella_{i}.cfg" for i in (4, 5, 6, 7)]
    blocks = parse_cfg_files(files)
    assert sorted(blocks) == [4, 5, 6, 7]
    assert {b["SNR"] for b in blocks.values()} == {"15"}
    assert {b["DM_MIN"] for b in blocks.values()} == {"20"}
    assert [blocks[j]["BEAM0"] for j in sorted(blocks)] == ["256", "320", "384", "448"]


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


def test_parse_ps_live_snapshot_fixture():
    """A real ``ps -eo args`` capture from corr1 (2026-09-08), unedited lines."""
    text = (FIXTURES / "ps" / "ps_eo_args_corr1_20260908.txt").read_text()
    info = parse_ps(text)
    assert info["utc_start"] == "2026-09-04-16:43:47"
    assert info["n_corr_dump"] == 1  # the binary, not its /bin/sh numactl wrapper
    assert info["n_bfcorr"] == 3 and info["n_sub_incoh"] == 3 and info["sub_incoh"] == 1
    assert info["bf_scale_factor"] == 8.0
    # four search binaries; the four fork wrappers and the four casm_hella.py
    # medusa daemons must not be counted
    assert info["n_hella"] == 4


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
    # three consecutive failures -> exactly one error event for the raiser
    failing = store.events(kind="collector_failing")
    assert {e["subject"] for e in failing} == {"bad"}
    assert all(e["severity"] == "error" for e in failing)
    assert len(failing) == 1
    # 'slow' timed out once and was then SKIPPED (its thread was still alive),
    # so it never got a second and third failure — that is the overlap guard.
    assert "timeout" in hb["slow"]["last_err"]
    assert [e["subject"] for e in store.events(kind="collector_overlap")] == ["slow"]
    runner.close()


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
        "kafka_bp",
        "zapdos",
        "disks",
        "gpus",
        "weights",
        "sky",
        "snapread",
        "vis",
        "search",
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
    store.set_watermark("zapdos", "last_probe_ts", time.time() - 2 * 3600.0)
    collector.collect(ctx)
    assert len(calls) == 2


def test_zapdos_interval_is_clamped_to_an_hour(settings, store: Store, monkeypatch):
    """A config asking for a 1 s interval still cannot probe more than hourly."""
    impatient = dataclasses.replace(settings, zapdos_min_interval_s=1.0)
    assert zapdos_interval_s(impatient) == 3600.0

    calls: list[list[str]] = []
    monkeypatch.setattr(
        "casm_monitor.collectors.services.run",
        lambda cmd, timeout=10.0: (calls.append(cmd), (0, "", ""))[1],
    )
    ctx = CollectorContext(settings=impatient, store=store, shards=ShardWriter(store))
    collector = ZapdosCollector(impatient)
    collector.collect(ctx)
    store.set_watermark("zapdos", "last_probe_ts", time.time() - 120.0)  # 2 min ago
    collector.collect(ctx)
    assert len(calls) == 1


def test_zapdos_slot_is_taken_atomically(settings, store: Store, monkeypatch):
    """Two collector instances racing on one store: exactly one probe."""
    calls: list[float] = []
    barrier = threading.Barrier(2)

    def fake_run(cmd, timeout=10.0):
        calls.append(time.time())
        return 0, "", ""

    monkeypatch.setattr("casm_monitor.collectors.services.run", fake_run)

    def probe():
        collector = ZapdosCollector(settings)
        ctx = CollectorContext(settings=settings, store=store, shards=ShardWriter(store))
        barrier.wait(timeout=10)
        collector.collect(ctx)

    threads = [threading.Thread(target=probe) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert len(calls) == 1


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
    monkeypatch.setattr(collector, "_read", lambda: parse_cfg_files({"/tmp/hella_0.cfg": HELLA_CFG}))
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


def test_hella_corr2_uses_the_marked_ssh_read(settings, store: Store, monkeypatch):
    """corr2 is read with the per-file marker command and parsed per file."""
    captured: dict[str, list[str]] = {}
    text = (FIXTURES / "hella" / "corr2_marked.txt").read_text()

    def fake_run(cmd, timeout=10.0):
        captured["cmd"] = cmd
        return 0, text, ""

    monkeypatch.setattr("casm_monitor.collectors.hella.run", fake_run)
    collector = HellaCollector(settings, node="corr2")
    ctx = CollectorContext(settings=settings, store=store, shards=ShardWriter(store))
    collector.collect(ctx)
    assert 'for f in /tmp/hella_*.cfg' in captured["cmd"][-1]
    assert store.latest_scalar("hella.corr2.n_jobs")["value"] == 4
    assert store.latest_scalar("hella.corr2.jobs")["value"] == "4,5,6,7"
    assert store.latest_scalar("hella.corr2.snr")["value"] == 15.0
    assert store.latest_scalar("hella.corr2.dm_min")["value"] == 20.0


# -- overlap guard ------------------------------------------------------
def test_timed_out_collector_is_not_started_again(settings, store: Store):
    """A collector still in its thread is skipped, not launched a second time."""

    class Stuck(Collector):
        name = "stuck"
        timeout_s = 0.05

        def __init__(self, s):
            super().__init__(s, cadence_s=0.01)
            self.entered = 0
            self.release = threading.Event()

        def collect(self, ctx: CollectorContext) -> None:
            self.entered += 1
            self.release.wait(timeout=10)

    stuck = Stuck(settings)
    runner = CollectorRunner(settings, [stuck], store=store)

    async def two_passes():
        assert await runner.run_once(stuck) is False  # timeout
        assert await runner.run_once(stuck) is False  # skipped, thread alive

    try:
        asyncio.run(two_passes())
        assert stuck.entered == 1
        overlaps = [
            r["value"]
            for r in store.query("SELECT value FROM scalars WHERE name = 'collector_overlap'")
        ]
        assert overlaps == [1]
        assert [e["subject"] for e in store.events(kind="collector_overlap")] == ["stuck"]

        # once the thread finishes, the collector runs again
        stuck.release.set()
        for _ in range(100):
            if runner._inflight["stuck"].done():
                break
            time.sleep(0.05)
        asyncio.run(runner.run_once(stuck))
        assert stuck.entered == 2
    finally:
        stuck.release.set()
        runner.close()


# -- kafka / weights conclusions ---------------------------------------
def test_kafka_ok_requires_every_topic(settings, store: Store, monkeypatch):
    """kafka_ok stays 0 while a configured topic has no end offsets."""
    from casm_monitor.collectors.services import ServicesCollector

    ctx = CollectorContext(settings=settings, store=store, shards=ShardWriter(store))
    collector = ServicesCollector(settings)

    class FakeConsumer:
        def __init__(self, offsets):
            self._offsets = offsets

        def partitions_for_topic(self, topic):
            return {0} if topic in self._offsets else set()

        def end_offsets(self, tps):
            return {tp: self._offsets[tp.topic] for tp in tps}

        def close(self, autocommit=False):
            pass

    def install(offsets):
        import sys
        import types

        module = types.ModuleType("kafka")
        module.KafkaConsumer = lambda **_kw: FakeConsumer(offsets)
        module.TopicPartition = lambda topic, p: type(
            "TP", (), {"topic": topic, "partition": p, "__hash__": lambda s: hash((topic, p))}
        )()
        monkeypatch.setitem(sys.modules, "kafka", module)

    topics = list(settings.kafka_topics)
    install({topics[0]: 10, topics[1]: 20})  # third topic missing
    collector._kafka(ctx)
    assert store.latest_scalar("services.kafka_ok")["value"] == 0
    assert topics[2] in store.latest_scalar("services.kafka_topics_missing")["value"]

    install({topics[0]: 11, topics[1]: 21, topics[2]: 31})
    collector._kafka(ctx)
    assert store.latest_scalar("services.kafka_ok")["value"] == 1

    # one topic stops moving: it is named, the other two stay advancing
    install({topics[0]: 12, topics[1]: 21, topics[2]: 32})
    collector._kafka(ctx)
    assert store.latest_scalar("services.kafka_advancing")["value"] == 0
    assert store.latest_scalar("services.kafka_stalled_topic")["value"] == topics[1]
    assert store.latest_scalar(f"kafka.{topics[0]}.advancing")["value"] == 1


def test_weights_missing_product_is_flagged_not_substituted(settings, store: Store, tmp_path):
    """A live event naming a product the registry lacks is a mismatch."""
    registry = tmp_path / "registry"
    (registry / "products").mkdir(parents=True)
    (registry / "products" / "OTHER0101.json").write_text(
        json.dumps({"product_id": "OTHER0101", "h5_path": "/a/newest.h5", "recorded_utc": "2026-09-07T00:00:00Z"})
    )
    (registry / "live_events.jsonl").write_text(
        json.dumps({"utc": "2026-09-08T00:00:00Z", "product_id": "GONE0908", "source": "deploy"}) + "\n"
    )
    local = dataclasses.replace(settings, registry_dir=registry)
    ctx = CollectorContext(settings=local, store=store, shards=ShardWriter(store))
    WeightsCollector(local).collect(ctx)

    assert store.latest_scalar("weights.product_missing")["value"] == 1
    assert store.latest_scalar("weights.registry_mismatch")["value"] == 1
    assert store.latest_scalar("weights.product_source")["value"] == "missing"
    # never the newest unrelated product
    assert store.latest_scalar("weights.product_id")["value"] == "unknown"
    assert store.latest_scalar("weights.weights_file")["value"] == "unknown"
    assert store.events(kind="registry_product_missing")[0]["detail"]["product_id"] == "GONE0908"
