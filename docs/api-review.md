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

## T1 scientific figures

The opening page and T1 page load rolling-24-hour plots automatically. Live
views refresh every two minutes while visible; selecting history pauses rolling.
`time_tz=America/Los_Angeles` (default) or `UTC` controls plot clock labels and
is echoed in the response and cache identity. API time bounds remain UTC.
The DM panel displays 0–1000 pc cm^-3 (`display_dm_max=1000`). Counts, stored
bins and JSON exports retain the full recorded range; search configuration
and the collector are unchanged. `display_note` discloses this distinction.

`GET /api/t1?t0=<ISO-or-unix>&t1=<ISO-or-unix>` defaults to rolling 24 hours,
supports at most seven days and reads the existing collector's `cand_bins`
table through a separate read-only SQLite connection. It streams at most
700,000 stored instance-gulp rows with an eight-second processing budget;
newest rows are retained first if either limit is reached. `partial` and the
reason are explicit, and the operator can narrow the interval. Missing tables
are `unavailable`; no stored bins is `empty`, not evidence of zero candidates.

Returned evidence includes counts, first/last recorded data, 480 display bins,
per-job maximum emitted candidates per instance-gulp in each display bin,
beam/time and DM/time histograms and width-index counts. DM edges and clipping
follow the existing collector. Width index is not labelled as FWHM. Acquisition
gaps and zero-candidate gulps absent from the source remain unknown.

**The fork's emitted candidates are clustered peaks.** These counts cannot
establish whether the pre-clustering 10,000-raw-peak cap was reached. No cap
fraction or skipped-beam estimate is derived from emitted counts, occupancy,
T2 job counts or the number of jobs reporting candidates.

The endpoint also reads at most the last 2 MiB of
`/data/casm/logs/bf_proc_hella.log`. Explicit `Only processed N/64 beams ...
10000 peaks` messages are preserved with the original line and UTC conversion
from the documented America/Los_Angeles log clock. This local tail is partial
coverage and is not a full-day or full-week saturation census. Unknown warning
absence remains unknown. Log timestamps do not identify the science sample
with sub-gulp precision; correlate them with observing context before inferring
a cause. No broad log scan or remote SSH is run by this route.

The response's `plot_url` points at a content-hashed dark Matplotlib PNG with
four scientific panels. The figure is downloadable and can be saved into an
investigation. `GET /api/review/{id}` exports the record as JSON.
`/api/t1/plot.png` is also a dynamic render endpoint, but is not
a valid immutable evidence URL for saving. These routes use no Plotly.

## Documentation impact and validation

This changes the observation denominator from UTC-today to rolling 24 hours,
introduces review-record persistence and clarifies emitted candidate versus
raw-peak semantics. Update central `guides/monitoring.md` and canonical wiki
operator-workflow requirements with the paired source/docs revision. Tests
cover rolling bounds, unchanged source stores, CSRF/origin, explicit requests,
pixel persistence, symlink escapes, incomplete bins and log-only cap evidence.
