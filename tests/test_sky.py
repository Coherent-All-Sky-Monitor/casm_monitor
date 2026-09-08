"""Sky collector against astropy: catalog keys, alt/az and next transit."""

from __future__ import annotations

import time

import numpy as np
import pytest

from casm_monitor.collectors.base import CollectorContext
from casm_monitor.collectors.sky import SOURCES, SkyCollector, next_altitude_maximum
from casm_monitor.store import ShardWriter, Store
from casm_monitor.util import parse_iso

sources_mod = pytest.importorskip("casm_vis_analysis.sources")


def test_catalog_keys_exist():
    """The names we ask for must be the exact keys casm_vis_analysis knows."""
    for name in SOURCES:
        if name == "sun":
            continue  # special-cased inside source_position
        assert name in sources_mod.CATALOG
    alt, az = sources_mod.source_altaz("sun", time.time())
    assert np.isfinite(alt).all() and np.isfinite(az).all()


def test_next_altitude_maximum_is_a_local_max():
    now = time.time()
    for name in SOURCES:
        t_max, alt_max = next_altitude_maximum(name, now)
        assert now <= t_max <= now + 86400 + 60
        alt_at, _az = sources_mod.source_altaz(name, t_max)
        assert float(np.atleast_1d(alt_at)[0]) == pytest.approx(alt_max, abs=1e-6)
        # a grid maximum: neighbours on the 1-min grid are not higher
        neighbours, _ = sources_mod.source_altaz(name, [t_max - 60.0, t_max + 60.0])
        assert alt_max >= max(neighbours) - 1e-6 or t_max <= now + 61


def test_sky_collector_writes_scalars(settings, store: Store):
    ctx = CollectorContext(settings=settings, store=store, shards=ShardWriter(store))
    SkyCollector(settings).collect(ctx)
    for name in SOURCES:
        alt = store.latest_scalar(f"sky.{name}.alt_deg")["value"]
        az = store.latest_scalar(f"sky.{name}.az_deg")["value"]
        assert -90.0 <= alt <= 90.0
        assert 0.0 <= az <= 360.0
        next_max = store.latest_scalar(f"sky.{name}.next_max_utc")["value"]
        assert parse_iso(next_max) is not None
        in_hr = store.latest_scalar(f"sky.{name}.next_max_in_hr")["value"]
        assert 0.0 <= in_hr <= 24.02
        assert store.latest_scalar(f"sky.{name}.up")["value"] in (0, 1)
        # alt agrees with a direct astropy call for the same instant
        row = store.latest_scalar(f"sky.{name}.alt_deg")
        direct, _ = sources_mod.source_altaz(name, row["ts"])
        assert float(np.atleast_1d(direct)[0]) == pytest.approx(alt, abs=0.02)
