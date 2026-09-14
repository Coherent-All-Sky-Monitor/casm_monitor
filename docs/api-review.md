# Search evidence and requested investigations

The operator workspace adds scientific T1 figures and a review queue. These
routes never run an LLM, post to Slack, change masks, generate calibration,
change beam membership or submit a telescope job. The production monitor and
T2 stores are opened read-only. Only an explicitly configured preview artifact
root may receive rendered products and investigation records.

## Injection review

`GET /api/observation` includes injection counts for a rolling 24-hour interval.
The start and end are explicit UTC strings; the seven-day trend retains UTC
calendar bins. `misses` contains all canonical search misses in the bounded
seven-calendar-day read, not only the thirty recent shots. The 5000-row budget
and incomplete-count flag apply to both. Unset outcomes and fire failures remain
separate from completed fired shots. Recovery evidence comes from the matched
search cluster; a synthetic replay is a separate artifact.

`GET /api/review` returns `items`, `csrf_token`, `writes_enabled`,
`source_status`, `injection_counts_complete` and the source-window start. With
`CASM_MONITOR_WORKSPACE=1` and an explicit isolated artifact root, refreshing the
queue mirrors every newly seen miss into a persistent **queued** local record.
This is a local evidence write, not an investigation or telescope operation.
The first-seen evidence remains queued after it ages out of the bounded source
window. Atomic insert-if-absent semantics never reset a human-requested record.
No record is deleted automatically. Empty or unavailable source evidence creates
no empty database. No background monitor is installed: misses that fall outside
the source window before any refresh are not claimed as captured.

Without workspace mode, GET remains read-only and source misses are virtual;
all review POSTs are refused. Existing persistent evidence is still readable.
`limit` (1 to 2000) and `offset` select saved records, most recently inserted
first. `saved_total`, `saved_truncated` and `next_offset` expose pagination;
reaching the display limit does not delete records or finish investigations.
Individual saved records remain addressable by ID regardless of list page.

## Save evidence and request an investigation

`POST /api/review` accepts:

```json
{
  "title": "Phase structure on a long N-S baseline",
  "note": "Inspect this selected interval",
  "selection": {"antennas": [9, 19], "t0": "2026-09-13T18:00:00Z"},
  "provenance": {"processing": "Sun fringe-stopped"},
  "plot_url": "/api/science/products/0123456789abcdef0123456789abcdef/plot-0.png"
}
```

The URL is illustrative. Only existing immutable science product PNGs or
`/api/t1/products/<sha256>.png` and immutable SNAP workspace spectra can be
snapshotted, not arbitrary URLs, paths or
dynamic render requests. Plot bytes are copied into the same SQLite transaction
as the record; SHA-256 and `saved_plot_url` are returned. The original image can
then disappear without breaking the record. Limit: 20 MiB PNG and 64 KiB JSON
per record, 200-character title and 8000-character note. Selection/provenance
submitted by the human are labelled as such, not independently verified facts.

The new record has `state="queued"`. Only
`POST /api/review/{id}/request` changes it to `requested`; repeated requests are
idempotent. A directly requested source miss is snapshotted into persistent
storage if not already mirrored, with its recorded injection evidence and an
unknown inferred cause. Requested
means waiting for an investigator, **not running**. There is no autonomous
runner attached. A saved plot is available at `/api/review/{id}/plot.png`.

Both POSTs require an exact same-origin `Origin` header and the token from GET
in `X-CASM-Review-CSRF`. These checks protect against cross-origin browser
requests, not against another trusted user of the SSH-forwarded endpoint.
The application additionally gates these narrow preview mutations separately
from telescope operations. Storage is `observation_cache_root/investigations.sqlite`;
there is no fallback to the production store when the root is unset.

## T1 stream monitoring

The T1 page answers one question first: are all eight Hella streams alive.
Streams 0-3 run on corr1, 4-7 on corr2, 64 beams each.

`GET /api/t1?t0=<ISO-or-unix>&t1=<ISO-or-unix>&time_tz=America/Los_Angeles|UTC`
defaults to a rolling 24 hours, supports at most seven days and refreshes every
five minutes (`refresh_s`). It reads two sources:

* the Hella gulp ledger, `observation_cache_root/hella_gulps.sqlite`, one row
  per gulp per stream tailed by byte watermark from
  `/data/casm/logs/bf_proc_hella.log` (override with `CASM_MONITOR_HELLA_LOG`,
  tick interval with `CASM_MONITOR_HELLA_LOG_INTERVAL_S`, default 60 s: the
  page refreshes every 5 minutes but a stream's reported staleness can never be
  fresher than the last log read). The tailer runs only in workspace mode,
  retains 14 days and never writes to the production store. Log timestamps are
  OVRO local and are converted to unix time on read;
* the collector's `cand_bins` table, which only holds rows for gulps that
  emitted candidates.

A gulp in the ledger with no `cand_bins` row is an EMPTY gulp: the normal
healthy state, shown as such. A time bin with no ledger row is "no gulp
recorded", which is the alarm condition. The two sources are counted per bin,
never matched gulp to gulp: `cand_bins.gulp_ts` is the data clock and the
ledger timestamp is the log wall clock.

Payload:

* `streams`: eight entries with `stream`, `node`, `last_gulp_unix`,
  `last_gulp_age_s`, `gulps_last_hour`, `expected_gulps_per_hour` (3600/8.59),
  `empty_fraction_last_hour`, `cap_hits_last_hour`, `median_wall_s_last_hour`
  and `status` (`ok` under 60 s, `late` under 600 s, else `silent`; `unknown`
  when the ledger has no rows for that stream). `last_gulp_age_s` is
  `last_tick_unix - last_gulp_unix` clamped at 0, the stream's staleness as of
  the last log read, not against wall clock: between ticks a wall-clock age
  would grow to the tick interval on every healthy stream.
* `ledger`: `status` (`ok`/`empty`/`missing_log`), `rows_in_window`,
  `newest_unix`, `log_path`, `watermark_offset`, `last_tick_unix` and
  `read_age_s` (now minus the last tick, so the UI can say "as of the log read
  N s ago").
* `activity`: `gulps`, `cands`, `gulps_with_cands` (count of `cand_bins` rows)
  and `cap_hits`, each `[480][8]` over the `time_edges_unix` grid (180 s bins
  over 24 hours).
* `beam_time_counts` `[480][512]`, `dm_time_counts` `[480][30]`, `quiet_bins`
  (480 flags: streams ran, nothing emitted), `dm_counts`, `width_counts`,
  `dm_edges`, `width_edges`, `n_candidates`, `t0`, `t1`, `time_tz`, `plot_url`.

`cand_bins` reads stay bounded: at most 700,000 rows with an eight-second
budget, after which `status` is `partial` and the operator narrows the
interval. A missing table is `unavailable` and still returns `streams`.

`plot_url` points at a content-hashed dark Matplotlib PNG with four panels:
gulp activity per stream, beam occupancy, DM over time (column-normalised, DM
10-3000 pc cm^-3) and the width and DM histograms. Liveness lives in the page's
HTML stream strip, not in the figure. Activity cells are coloured by
`gulps_with_cands / gulps`, slate at 0 (healthy) to cyan at 1, with no-gulp
bins transparent and cap hits overlaid in orange; a per-bin "any candidate"
flag saturates at 21 gulps per bin and 90% empty gulps.
`/api/t1/plot.png` renders the same figure dynamically and is not a valid
immutable evidence URL for saving. These routes use no Plotly. Width index is
not labelled as FWHM.

## Documentation impact and validation

This changes the observation denominator from UTC-today to rolling 24 hours,
introduces review-record persistence and clarifies emitted candidate versus
raw-peak semantics. Update central `guides/monitoring.md` and canonical wiki
operator-workflow requirements with the paired source/docs revision. Tests
cover rolling bounds, unchanged source stores, CSRF/origin, explicit requests,
pixel persistence, symlink escapes, incomplete bins and log-only cap evidence.
