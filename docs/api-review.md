# Search evidence and requested investigations

The operator workspace adds scientific T1 figures and a review queue. These
routes never run an LLM, post to Slack, change masks, generate calibration,
change beam membership or submit a telescope job. The production monitor and
T2 stores are opened read-only. Only an explicitly configured preview artifact
root may receive rendered products and investigation records.

## Injection review

The **Overview** tab at `/observation` replaces Injection recovery. Four live
cards show observation state, Hella streams, rolling-24-hour injection recovery
and cached-visibility age. The live cards refresh every 30 seconds while visible;
state and ledger evidence older than 120 seconds cannot stay green. Visibility
age above 10 minutes is marked stale; this is a display threshold, not an
instrument-quality test. Missing, pending and incomplete evidence remain explicit.
History panels share a time-zone and interval control, default rolling 24 hours
with two-minute refresh. Historical selections pause those panels, not live cards.
The live recovery card uses the operator's absolute recovered-count thresholds:
18 or more is yellow, labelled **Needs attention** (including 24/24), fewer than 12 is red; the intermediate
12–17 band is orange. These are 24-hour display bands, not recovery percentages
or scientific acceptance criteria. Unavailable/empty evidence stays unknown;
stale or incomplete counts take priority over count bands. Individual trial
misses remain red; shorter historical intervals do not use these thresholds.

Injection outcomes use a white click-to-zoom timeline. Trial markers open their
details inside the enlarged view. The trial-history/seven-day table disclosure
is removed; API evidence remains available. All displayed data figures share
Fit, +/−, double-click zoom and pan, including saved review evidence and candidate
statistics. Selection maps retain their own interactions.
The adjacent current-layout map reuses the
Visibilities inspection set and labels wired, intended and inspected membership
separately. It shows configuration, not antenna health. Recorded weights membership
and the current ledger calibration do not establish runtime activation.
The bottom row contains only the stream-count heatmap and a raw reference-baseline
dynamic spectrum with amplitude/phase selection. Images and the timeline enlarge.
Search and detailed-visibility links carry the selected history interval.
The header's top-right CASM photo is a bundled, operator-supplied still image,
not a live camera. Its full uncropped frame opens in the shared zoom viewer.
The telescope title is **Coherent All Sky Monitor (CASM)**; its location is
shown separately as Owens Valley Radio Observatory, Big Pine, California.

`GET /api/observation` includes injection counts for a rolling 24-hour interval.
The start and end are explicit UTC strings; the seven-day trend retains UTC
calendar bins. `misses` contains all canonical search misses in the bounded
seven-calendar-day read, not only the thirty recent shots. The 5000-row budget
and incomplete-count flag apply to both. Unset outcomes and fire failures remain
separate from completed fired shots. Recovery evidence comes from the matched
search cluster; a synthetic replay is a separate artifact.

`GET /api/observation/injections?t0=<ISO-or-unix>&t1=<ISO-or-unix>` selects a
finite positive interval of at most seven days, defaulting to rolling 24 hours.
`trials` contains every retained shot in that interval, not the thirty-row
`recent` sample, and omits arbitrary recorded replay paths. `counts` uses the
same selection. UTC-day trend bins end on the selected endpoint's date, while
`as_of_utc` records the actual read time. The read covers the union of that
interval and its seven-day trend, with the same 5000-row budget and partial flag.
History selection cannot change the live cards or initiate an injection.

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

The **Search (T1)** page at `/search` answers one question first: are all eight Hella streams alive.
Streams 0-3 run on corr1, 4-7 on corr2, 64 beams each.

`GET /api/t1/status` reads only current gulp-ledger status for Overview, without
candidate-array reads or figure rendering. `GET /api/t1?compact=true` returns an
immutable `activity_plot_url` for the first panel alone, with exactly the full
figure's counts, zero/missing masks, cap strips and shared logarithmic scale.
The default API/figure is unchanged. `/search?t0=...&t1=...&time_tz=UTC` opens the
requested historical interval with rolling paused.

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

`plot_url` points at a content-hashed white-background Matplotlib PNG with five panels:
candidates per stream, beam occupancy, DM over time (DM 10–1000 pc cm^-3)
and the width and DM histograms. Liveness lives in the page's HTML stream strip.
Stream and DM heatmaps show candidate counts per time bin on logarithmic colour
scales, from pale lavender for low counts to deep purple for high counts.
Lightness decreases throughout the ramp. All three time panels share
the same palette and limits: 1 to the largest displayed count in any panel
(2 when all counts are zero or one). Ticks show decades and the exact maximum,
formatted as integers; nearby decade labels are omitted to avoid crowding the
maximum label. Minor ticks are hidden. The default 24-hour window uses
3-minute bins; all three time-panel colourbar labels follow the selected bin
duration. DM counts combine all streams and are counts per DM bucket, not a
density or fraction. The stream title includes the approximate 8.59-second gulp.
All three time panels use off-white for zero recorded candidates when a gulp
is recorded and grey for no recorded gulp or candidates. Beam cells use
their parent stream's gulp evidence; DM cells use evidence from any stream.
These colours do not establish individual beam processing or full coverage.
Positive candidate counts remain visible even without matching gulp evidence.
Narrow red strips mark logged cap hits without
hiding candidate counts. The top y-axis is stream ID (0–7), not beam
ID: each row combines 64 beams. Red occupies the upper edge of the affected
stream row and flags at least one capped gulp in the time bin; its vertical
position does not identify skipped beams. Counts reflect emitted candidates
from the beams actually processed, without correcting for skipped beams.
Colourbar labels use two lines to fit within their panels. The same zero/missing
colours also appear for intervals without any candidates. Both DM axes use
logarithmic spacing from 10–1000. The bottom histogram retains linear counts,
labelled "Candidates", and the existing logarithmically spaced bins.
The saved 0–10 bucket is omitted from both DM displays and their scaling,
but retained in evidence JSON and the stream/beam candidate totals.
The stored edges are unchanged, including 921.750557–1122.099537 across the
display ceiling. Its count stays intact while the axis ends at 1000; the figure
does not infer counts on either side of that edge. All bins remain in evidence
JSON, including bins outside the displayed range for historical intervals.
The September 25 read-only check confirmed DM_MIN 0 and DM_MAX 1000 in all
eight corr1/corr2 `/tmp/hella_N.cfg` files. No search configuration changed.
`/api/t1/plot.png` renders the same figure dynamically and is not a valid
immutable evidence URL for saving. These routes use no Plotly. The width
histogram shows indices 0–6 with integer ticks and linear counts, matching
the current Hella trials. Spare width bins 7–8 stay in stored/API evidence,
outside the displayed bars and their scaling. Width index is not labelled as FWHM.

The figure matches the Visibilities white scientific style: dark labels and
spines, subtle time/histogram grids, muted-blue histogram bars, explicit time zones
and units, beam-index ticks at stream boundaries, and readable 10–1000 DM
ticks. All three time panels retain their shared logarithmic
count palette and limits. The PNG is 1950×1875 pixels; click it to open the
shared display-zoom viewer with Fit, +/− and pan. Zoom pins the selected PNG
and interval, without new queries, rebinning or count normalization. Downloads,
immutable investigation snapshots and the same-interval visibility link remain.
The page retains rolling 24 hours with five-minute refresh. Off-white stream
cards use dark text, explicit last-gulp and rate labels, and full-width solid
status bands with white labels and thicker matching top borders: green OK,
amber late, red silent, grey unknown.
Missing ages/fractions remain `no gulps`/`n/a`; empty gulps do not imply a stopped
stream. Cards wrap from eight to four or two columns on narrow screens.
Search settings, candidate counts,
the gulp ledger and production services are unchanged by this presentation update.

`tests/test_t1.py` checks counts, cap overlays, zero/missing states, units,
tick/label bounds, white PNG backgrounds and temporary style isolation.
`scripts/check_search_browser.py` checks the name, immutable-image zoom,
keyboard/focus behaviour, UTC/history controls, downloads and mobile overflow.
It also checks all four card states, text contrast and seven viewport widths.
Cards fit at 320 px; whole-page overflow is checked from 390 px because the
existing global navigation is wider than a 320 px viewport.

## Documentation impact and validation

This changes the observation denominator from UTC-today to rolling 24 hours,
introduces review-record persistence and clarifies emitted candidate versus
raw-peak semantics. Update central `guides/monitoring.md` and canonical wiki
operator-workflow requirements with the paired source/docs revision. Tests
cover rolling bounds, unchanged source stores, CSRF/origin, explicit requests,
pixel persistence, symlink escapes, incomplete bins and log-only cap evidence.
