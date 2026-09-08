"""Config loading, env overrides and medusa.cfg parsing."""

from __future__ import annotations

from pathlib import Path

from casm_monitor.config import (
    DEFAULT_CONFIG_PATH,
    Settings,
    load_settings,
    medusa_endpoints,
    read_medusa_cfg,
)


def test_ships_defaults_file():
    assert DEFAULT_CONFIG_PATH.is_file(), "config/monitor.yaml must be shipped"


def test_load_shipped_defaults(monkeypatch):
    for key in (
        "CASM_MONITOR_STORE_ROOT",
        "CASM_MONITOR_WEB_HOST",
        "CASM_MONITOR_WEB_PORT",
        "CASM_MONITOR_ALLOW_UPLOAD",
        "CASM_MONITOR_CONFIG",
    ):
        monkeypatch.delenv(key, raising=False)
    s = load_settings()
    assert s.store_root == Path("/mnt/nvme3/casm_monitor")
    assert (s.web_host, s.web_port) == ("127.0.0.1", 8060)
    assert s.vis_dir == Path("/mnt/nvme4/data/casm/visibilities_64ant")
    assert s.hella_cands_dir == Path("/mnt/nvme4/data/casm/hella_cands")
    assert s.t2_db == Path("/mnt/nvme5/casm_pipeline/db/t2.sqlite")
    assert s.registry_dir == Path("/mnt/nvme5/casm_pipeline/weights/registry")
    assert s.deployed_weights_csv == Path("/home/casm/software/dev/casm-wiki/deployed_weights.csv")
    assert s.kafka_bootstrap == "casm-corr1:9092"
    assert "casm_antenna_bp" in s.kafka_topics
    assert s.zapdos_ssh == "zapdos"
    assert s.allow_upload is False
    assert s.cadence("sky") == 60.0
    assert s.db_path == s.store_root / "monitor.sqlite"


def test_env_config_path_and_overrides(tmp_path, monkeypatch):
    cfg = tmp_path / "m.yaml"
    cfg.write_text(
        "store_root: /tmp/does-not-matter\n"
        "web:\n  host: 127.0.0.1\n  port: 9\n"
        "cadences:\n  obs: 7\n"
        "shard_ttl_days:\n  demo: 2\n"
        "allow_upload: false\n"
    )
    monkeypatch.setenv("CASM_MONITOR_CONFIG", str(cfg))
    monkeypatch.setenv("CASM_MONITOR_STORE_ROOT", str(tmp_path / "store"))
    monkeypatch.setenv("CASM_MONITOR_WEB_PORT", "8060")
    monkeypatch.setenv("CASM_MONITOR_ALLOW_UPLOAD", "1")
    s = load_settings()
    assert s.config_path == cfg
    assert s.store_root == tmp_path / "store"
    assert s.web_port == 8060
    assert s.cadence("obs") == 7.0
    assert s.shard_ttl_s("demo") == 2 * 86400.0
    assert s.allow_upload is True


def test_missing_file_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("CASM_MONITOR_STORE_ROOT", raising=False)
    s = load_settings(tmp_path / "nope.yaml")
    assert s.config_path is None
    assert s.store_root == Settings().store_root


def test_read_medusa_cfg(tmp_path):
    p = tmp_path / "medusa.cfg"
    p.write_text(
        "# comment\nREDIS_HOST                casm-corr1\nREDIS_PORT   6379\n"
        "REDIS_PW   sekrit\nLMC_PORT   20300\nKAFKA_PORT  9092\nSERVER_HOST casm-corr1\n"
    )
    cfg = read_medusa_cfg(p)
    assert cfg["REDIS_HOST"] == "casm-corr1"
    ep = medusa_endpoints(Settings(medusa_cfg=p))
    assert (ep.redis_host, ep.redis_port, ep.redis_password) == ("casm-corr1", 6379, "sekrit")
    assert (ep.lmc_host, ep.lmc_port) == ("casm-corr1", 20300)


def test_no_secret_in_repo():
    """The redis password must never appear in a tracked file."""
    needle = "frbs" + "2025"  # assembled so this file is not itself a hit
    repo = Path(__file__).resolve().parent.parent
    hits = []
    for sub in ("casm_monitor", "config", "deploy", "tests", "docs"):
        for path in (repo / sub).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".yaml", ".yml", ".md", ".service", ".sh"}:
                if needle in path.read_text(errors="replace"):
                    hits.append(str(path))
    assert hits == []
