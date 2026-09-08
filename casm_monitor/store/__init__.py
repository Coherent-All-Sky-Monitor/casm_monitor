"""Monitor history store: SQLite scalars/events/jobs + immutable zarr shards."""

from .db import Store
from .schema import SCHEMA, SCHEMA_VERSION
from .shards import (
    ShardReader,
    ShardWriter,
    UnsafePathError,
    add_shard_ref,
    apply_retention,
    drop_shard_ref,
    ensure_contained,
    iso_compact,
    referenced_shard_ids,
    safe_name,
    store_disk_usage,
)

__all__ = [
    "Store",
    "SCHEMA",
    "SCHEMA_VERSION",
    "ShardReader",
    "ShardWriter",
    "UnsafePathError",
    "add_shard_ref",
    "apply_retention",
    "drop_shard_ref",
    "ensure_contained",
    "iso_compact",
    "referenced_shard_ids",
    "safe_name",
    "store_disk_usage",
]
