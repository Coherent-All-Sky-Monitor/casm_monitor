# Observation overview

`GET /api/observation` returns the site clock, observation freshness, array
layout, recorded deployment membership, solar context and injection recovery.
It reads small records and cached products. It never opens raw visibility files
or inspects the weights payload during a request.

The three membership concepts remain separate: wiring (`functional`), intended
participation (`include_in_beamforming`) and nonzero int8 slots in each beam of
the recorded deployed product. The worker scans all stored frequencies and
polarizations, bounded to 400 MiB uncompressed weights, and caches the result by
product ID, resolved path, file size and modification time. Missing or changed
products return unknown membership. Global HDF5 `active_mask` is exposed as
metadata, not substituted for the payload-derived beam membership.

Latest registry evidence is resolved separately for each stream from a bounded
1 MiB log tail. All six must name the same product and match the collector
association before a deployed union is reported. This preserves dated registry
evidence; it does not constitute a new audit of running node memory. Coordinates
from the current layout and the weights product are separate because antenna
IDs and geometry can change between layout epochs.

Solar context uses the existing shared `casm_vis_analysis.solar_waterfall`
renderer with 24 hours of `vis_avg8` cache for antennas 9 and 19, resolved through
`AntennaMapping`. These IDs are an explicit baseline choice, not an active-count
assumption. It refuses missing baseline wiring, mixed frequency axes, more than
1024 integration samples or 393216 selected cells. It disables whole-shard
fallback reads. No raw data is read. The figure shows raw correlated amplitude,
with each channel divided by its mean over the displayed interval, and uses
the cache's existing 8-channel complex averages. It is not a solar flux or
beamformed-power measurement. Other sky sources and interference contribute.
The plot uses stored integration timestamps and nominal integration widths;
absolute integration start/centre/end semantics are not independently verified.

The existing `render_figures` worker includes an `observation` target and writes
atomic manifests/images. Acquisition times and render times are distinct;
failed refreshes retain the last image with failure/staleness information.
`GET /api/observation/solar.png` serves only that cached file with HTTP caching.

For an isolated preview, set `CASM_MONITOR_OBSERVATION_ROOT` to a separate
directory and `CASM_MONITOR_READ_ONLY=1`. The web app opens SQLite read-only and
rejects write HTTP methods. Run it on a separate port; do not start collectors
or workers. A bounded manual render may call `render_observation` with a
read-only production `Store` and preview output settings. No service restart,
data acquisition, calibration build or deployment follows from viewing it.

Mean LST uses installed offline Earth-orientation tables with downloads disabled.
It is a display clock, not a precision calibration astrometry contract.

## Injection records and saved evidence

The overview queries the existing T2 database with SQLite URI `mode=ro` and
`query_only=ON`; it never uses the schema-migrating T2 connection helper.
Counts cover today in UTC and trends cover seven UTC calendar days, capped at
5000 rows with an explicit partial status beyond the cap. The response includes
30 recent trials and the latest completed trial. Completed fired trials form
the recovery denominator; firing failures, pending/legacy and unknown outcomes
remain separate. No ledger means unavailable, not a measured zero.

`GET /api/observation/injections/artifact/{file_id}/{kind}` serves existing
PNG, JSON or filterbank files from fixed event paths under `/mnt/nvme3/T3/EVENTS`.
It rejects malformed IDs, arbitrary kinds and symlink escapes. A replay is a
synthetic pulse re-added to recorded background; matching search evidence
establishes live recovery. A missing replay does not invalidate recovery.
No endpoint renders a replay, requests a dump, labels an event or sends Slack.

## Documentation impact

This change adds a read-only overview API and preview option, changes default
navigation and figure refreshing, and adds a shared prepared-array solar
renderer. Central documentation: `casm-software-docs/docs/guides/monitoring.md`,
`guides/solar-waterfall.md`, package API/source snapshot and
`docs/maintaining-docs.md`. Existing science examples retain historical inputs.
Cross-day calibration, Cyg A modeling and layout-policy implementation remain
deferred. Fourier Space/Kafka code and production observation state are unchanged.
