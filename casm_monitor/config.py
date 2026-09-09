"""Settings loaded from ``config/monitor.yaml``.

The YAML file is the single source of paths and cadences. A few keys can be
overridden by environment variables so that a test or a smoke run can point the
service at a scratch store without editing the file:

    CASM_MONITOR_CONFIG        path of the YAML file itself
    CASM_MONITOR_STORE_ROOT    store_root
    CASM_MONITOR_WEB_HOST      web.host
    CASM_MONITOR_WEB_PORT      web.port
    CASM_MONITOR_ALLOW_UPLOAD  allow_upload ("1"/"true" -> True)

Secrets are never stored in the repo: the redis password and the LMC port come
from medusa.cfg at runtime (:func:`read_medusa_cfg`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "monitor.yaml"

DEFAULT_CADENCES: dict[str, float] = {
    "obs": 30.0,
    "hella": 60.0,
    "hella_corr2": 600.0,
    "services": 30.0,
    "zapdos": 3600.0,
    "disks": 300.0,
    "gpus": 60.0,
    "weights": 120.0,
    "sky": 60.0,
    "kafka_bp": 1.0,        # one poll per second inside the runner
    "kafka_bp_frame": 30.0,  # frame cadence used to grade the status strip
    "snapread": 60.0,
    "store": 300.0,
}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Immutable service settings."""

    store_root: Path = Path("/mnt/nvme3/casm_monitor")
    web_host: str = "127.0.0.1"
    web_port: int = 8060
    vis_dir: Path = Path("/mnt/nvme4/data/casm/visibilities_64ant")
    hella_cands_dir: Path = Path("/mnt/nvme4/data/casm/hella_cands")
    t2_db: Path = Path("/mnt/nvme5/casm_pipeline/db/t2.sqlite")
    registry_dir: Path = Path("/mnt/nvme5/casm_pipeline/weights/registry")
    deployed_weights_csv: Path = Path("/home/casm/software/dev/casm-wiki/deployed_weights.csv")
    medusa_cfg: Path = Path("/home/casm/software/fourier-space/opt/casm/share/common/medusa.cfg")
    kafka_bootstrap: str = "casm-corr1:9092"
    kafka_topics: tuple[str, ...] = ("casm_antenna_bp", "casm_antenna_ts", "casm_antenna_hg")
    corr2_ssh: str = "casm-corr2"
    zapdos_ssh: str = "zapdos"
    check_head_node: bool = False
    disks: tuple[str, ...] = ("/mnt/nvme3", "/mnt/nvme4", "/mnt/nvme5")
    cadences: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_CADENCES))
    zapdos_min_interval_s: float = 3600.0
    # SNAP board reads (zapdos). The antenna board list is never duplicated in
    # the YAML: it is read from ``snap_map_csv`` at runtime.
    snap_map_csv: Path = Path("/home/casm/software/dev/antenna_layouts/casm_snap_map.csv")
    # Same file board_table() reads (casm_monitor.collectors.rowmap.LAYOUT_CSV):
    # the ``current`` symlink, never the stale antenna_layout_current.csv.
    snap_layout_csv: Path = Path("/home/casm/software/dev/antenna_layouts/current")
    snap_antenna_boards: tuple[str, ...] = ()  # empty = take them from the CSV
    snap_relay_boards: tuple[str, ...] = (
        "192.168.120.59",
        "192.168.120.68",
        "192.168.120.69",
    )
    snap_read_interval_s: float = 3600.0
    snap_manual_min_interval_s: float = 300.0
    snap_per_board_timeout_s: float = 60.0
    shard_ttl_days: dict[str, float] = field(default_factory=dict)
    allow_upload: bool = False
    config_path: Path | None = None

    # --- derived paths -------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.store_root / "monitor.sqlite"

    @property
    def shards_root(self) -> Path:
        return self.store_root / "shards"

    @property
    def jobs_root(self) -> Path:
        return self.store_root / "jobs"

    def cadence(self, name: str, default: float = 60.0) -> float:
        return float(self.cadences.get(name, DEFAULT_CADENCES.get(name, default)))

    def shard_ttl_s(self, stream: str) -> float | None:
        days = self.shard_ttl_days.get(stream)
        return None if days is None else float(days) * 86400.0


def config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve which YAML file to load."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("CASM_MONITOR_CONFIG")
    if env:
        return Path(env)
    return DEFAULT_CONFIG_PATH


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """Load settings from YAML, then apply environment overrides.

    A missing file is not an error: the dataclass defaults are the production
    values, so the service still starts (and the config path is reported as
    None).
    """
    p = config_path(path)
    raw: dict[str, Any] = {}
    if p.is_file():
        raw = yaml.safe_load(p.read_text()) or {}

    paths = raw.get("paths") or {}
    web = raw.get("web") or {}
    kafka = raw.get("kafka") or {}
    hosts = raw.get("hosts") or {}
    snap = raw.get("snap") or {}
    # ``antenna_boards`` is either ``{from_csv: <path>}`` (the normal case: the
    # boards come from the SNAP map at runtime) or an explicit list of IPs used
    # to override it. Only the second form ends up in ``snap_antenna_boards``.
    snap_antenna_boards = snap.get("antenna_boards")
    if isinstance(snap_antenna_boards, dict):
        from_csv = snap_antenna_boards.get("from_csv")
        if from_csv:
            snap["snap_map_csv"] = from_csv
        snap_antenna_boards = ()
    elif snap_antenna_boards is None:
        snap_antenna_boards = ()
    cadences = dict(DEFAULT_CADENCES)
    cadences.update({str(k): float(v) for k, v in (raw.get("cadences") or {}).items()})

    defaults = Settings()
    kw: dict[str, Any] = dict(
        store_root=Path(raw.get("store_root", defaults.store_root)),
        web_host=str(web.get("host", defaults.web_host)),
        web_port=int(web.get("port", defaults.web_port)),
        vis_dir=Path(paths.get("vis_dir", defaults.vis_dir)),
        hella_cands_dir=Path(paths.get("hella_cands_dir", defaults.hella_cands_dir)),
        t2_db=Path(paths.get("t2_db", defaults.t2_db)),
        registry_dir=Path(paths.get("registry_dir", defaults.registry_dir)),
        deployed_weights_csv=Path(paths.get("deployed_weights_csv", defaults.deployed_weights_csv)),
        medusa_cfg=Path(paths.get("medusa_cfg", defaults.medusa_cfg)),
        kafka_bootstrap=str(kafka.get("bootstrap", defaults.kafka_bootstrap)),
        kafka_topics=tuple(kafka.get("topics", defaults.kafka_topics)),
        corr2_ssh=str(hosts.get("corr2_ssh", defaults.corr2_ssh)),
        zapdos_ssh=str(hosts.get("zapdos_ssh", defaults.zapdos_ssh)),
        check_head_node=_as_bool(hosts.get("check_head_node", defaults.check_head_node)),
        disks=tuple(raw.get("disks", defaults.disks)),
        cadences=cadences,
        # Clamped: zapdos is contacted at most once per hour, so a config that
        # asks for less is silently raised to the floor (the collector clamps
        # again in code and takes the slot with an atomic compare-and-set).
        zapdos_min_interval_s=max(
            float(raw.get("zapdos_min_interval_s", defaults.zapdos_min_interval_s)), 3600.0
        ),
        snap_map_csv=Path(snap.get("snap_map_csv", defaults.snap_map_csv)),
        snap_layout_csv=Path(snap.get("layout_csv", defaults.snap_layout_csv)),
        snap_antenna_boards=tuple(str(x) for x in snap_antenna_boards),
        snap_relay_boards=tuple(str(x) for x in snap.get("relay_boards", defaults.snap_relay_boards)),
        # Clamped exactly like the zapdos probe: the hour is a floor, a config
        # that asks for less is raised to it (the collector clamps again).
        snap_read_interval_s=max(
            float(snap.get("read_interval_s", defaults.snap_read_interval_s)), 3600.0
        ),
        snap_manual_min_interval_s=float(
            snap.get("manual_min_interval_s", defaults.snap_manual_min_interval_s)
        ),
        snap_per_board_timeout_s=float(
            snap.get("per_board_timeout_s", defaults.snap_per_board_timeout_s)
        ),
        shard_ttl_days={str(k): float(v) for k, v in (raw.get("shard_ttl_days") or {}).items()},
        allow_upload=_as_bool(raw.get("allow_upload", defaults.allow_upload)),
        config_path=p if p.is_file() else None,
    )

    if os.environ.get("CASM_MONITOR_STORE_ROOT"):
        kw["store_root"] = Path(os.environ["CASM_MONITOR_STORE_ROOT"])
    if os.environ.get("CASM_MONITOR_WEB_HOST"):
        kw["web_host"] = os.environ["CASM_MONITOR_WEB_HOST"]
    if os.environ.get("CASM_MONITOR_WEB_PORT"):
        kw["web_port"] = int(os.environ["CASM_MONITOR_WEB_PORT"])
    if os.environ.get("CASM_MONITOR_ALLOW_UPLOAD"):
        kw["allow_upload"] = _as_bool(os.environ["CASM_MONITOR_ALLOW_UPLOAD"])

    return Settings(**kw)


def read_medusa_cfg(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse medusa.cfg (``KEY   value`` lines, ``#`` comments).

    Read-only: the file belongs to Fourier Space and is never written here.
    """
    out: dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return out
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            out[parts[0]] = parts[1].strip()
    return out


@dataclass(frozen=True)
class MedusaEndpoints:
    """The handful of medusa.cfg values the collectors need."""

    redis_host: str = "casm-corr1"
    redis_port: int = 6379
    redis_password: str | None = None
    lmc_host: str = "casm-corr1"
    lmc_port: int = 20300
    kafka_host: str = "casm-corr1"
    kafka_port: int = 9092


def medusa_endpoints(settings: Settings) -> MedusaEndpoints:
    """Read redis/LMC/kafka endpoints out of medusa.cfg.

    The password is returned in memory only; it is never logged, never written
    to the store and never appears in the repo.
    """
    cfg = read_medusa_cfg(settings.medusa_cfg)
    default = MedusaEndpoints()
    return MedusaEndpoints(
        redis_host=cfg.get("REDIS_HOST", default.redis_host),
        redis_port=int(cfg.get("REDIS_PORT", default.redis_port)),
        redis_password=cfg.get("REDIS_PW") or None,
        lmc_host=cfg.get("SERVER_HOST", default.lmc_host),
        lmc_port=int(cfg.get("LMC_PORT", default.lmc_port)),
        kafka_host=cfg.get("KAFKA_HOST", default.kafka_host),
        kafka_port=int(cfg.get("KAFKA_PORT", default.kafka_port)),
    )
