"""casm_monitor: an independent monitoring service for the CASM array.

Three processes share one on-disk store (SQLite + immutable zarr shards under
``store_root``):

* ``casm-monitor-collect`` — asyncio collector runner, the only writer of
  scalars/events/shards.
* ``casm-monitor-web`` — FastAPI app on 127.0.0.1:8060, reads the store
  read-only and writes only the jobs and events tables.
* ``casm-monitor-jobs`` — single job worker over the SQLite job queue.

The service is strictly additive to the medusa / Fourier-Space flow: it never
touches fourier-space code or configs, reads Redis read-only, consumes Kafka
group-less without committing offsets, and has no code path to any SNAP
programming call.
"""

__version__ = "0.1.0"
