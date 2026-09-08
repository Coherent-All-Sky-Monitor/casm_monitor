"""Shared fixtures. Every test store lives under tmp_path; never /mnt."""

from __future__ import annotations

from pathlib import Path

import pytest

from casm_monitor.config import Settings
from casm_monitor.store import ShardWriter, Store


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        store_root=tmp_path / "store",
        web_host="127.0.0.1",
        web_port=8060,
        cadences={"obs": 30.0, "services": 30.0, "sky": 60.0, "disks": 300.0, "store": 300.0},
        shard_ttl_days={"demo": 1.0},
    )


@pytest.fixture
def store(settings: Settings) -> Store:
    st = Store(settings.db_path, store_root=settings.store_root)
    yield st
    st.close()


@pytest.fixture
def shard_writer(store: Store, settings: Settings) -> ShardWriter:
    return ShardWriter(store, settings.shards_root)
