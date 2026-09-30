# Visibility history playback

Open **Visibilities → History → Video playback**. Live remains the default;
opening history stops the array page's minute refresh. The player initially
uses the selected antennas, reference, quantity, band, processing, arrangement,
spectrum statistic and panel window. The initial period is the last 24 hours
ending at the saved snapshot, not the browser clock. Choose 48 hours, Last week,
or a UTC end date; historical dates end just before the following midnight.

Play/pause, previous/next integration, timeline, 0.5–4× speed and Loop operate on
saved integrations only. 1× is one integration per second of playback, not
real time. The header shows saved UTC, OVRO local and mean LST simultaneously.
LST uses the same offline Astropy/OVRO convention as Overview. If unavailable,
it is labelled unavailable. No time samples are synthesized across gaps.

## Plots and references

- Spectrum: one integration, or an explicit rolling-window average. Magnitude
  averages magnitudes; Real/Imag average complex values; Phase is the angle of
  that complex mean. Exact-zero phase is undefined. Full cached frequency
  resolution is retained (384 channels in the production avg8 cache).
- Dynamic spectrum: a moving panel window ending at the saved playhead. Its
  duration is independent of the playback period. Frequency is averaged to
  at most 128 channels using the existing preview estimators; every time sample
  is retained. Dark pixels mean missing/undefined values.
- All pairs: dynamic spectra in an upper triangle, autos on the diagonal.
  The lower triangle is intentionally blank. Reordering the matrix also
  conjugates reversed pairs so cells still mean V(row, column).

Compact panels fill the playback viewport in plank order. SNAP order sorts by
the dated layout's SNAP/slot/ADC fields. Station grid preserves empty and hidden
positions. The expandable antenna map distinguishes current recorded
beamforming members from other wired inputs; it is not historical membership.

For reference R, cross panels show V(target, R), including R's auto if selected.
Changing reference pauses, preserves the selected timestamp and options, and
buffers the new baselines. Old-reference plots are never shown under the new
reference label. Recently loaded references remain in a byte-bounded browser
cache. Quantity/order switches use buffered data; they do not reread the store.
Raw and Sun fringe-stopped are supported. No calibration or normalization is
silently applied. The avg8 cache already contains eight-channel complex means
before any optional Sun fringe stop, as in the ordinary array inspector.

Axes stay fixed through the sequence. Spectrum limits include all finite values
in the playback period and preceding context, with padding; signed quantities
have symmetric limits and phase is −π to π. Dynamic amplitude uses fixed
2nd/98th percentiles, signed colours fixed symmetric 98th-percentile limits,
and phase the fixed cyclic scale. Percentiles use a deterministic sample of
at most 128 integrations across the loaded period, not each frame. Colour
clipping is a display scale, not data normalization. Colourbars remain labelled.

Click any panel or matrix cell to pause and open the shared zoom/download viewer.
The enlarged view retains the saved clocks, axes and colourbar. **Open stored-pair
detail** selects that exact historical window in the detailed inspector; its
ascending stored-pair convention may reverse Imag/Phase relative to the movie.

## Read-only API and budgets

`GET /api/science/array/history` reports cached availability.

`GET /api/science/array/playback` accepts comma-separated packet `inputs`,
`mode=auto|cross|matrix`, `reference_input`, `reference=raw|sun`, `span_days=1|2|7`,
`window_hours=0.1..24`, optional Unix `end`, and MHz `fmin`/`fmax`. The response
pins exact shard identities, timestamps, dated geometry, pair direction,
frequency axes, scales and saved LST. Maximum 32 inputs and 5,500 integrations,
including look-back. Layout/frequency changes require splitting the selection.
Missing, duplicate or inconsistent metadata is rejected; no timestamps are
invented. Missing baselines remain NaN. Expired files/sessions produce errors,
not a raw-file fallback. Pins are in memory, not retention writes to the DB.

`GET /api/science/array/playback-chunk?id=…&base=…&encoding=complex|preview`
returns at most 32 integrations. `base` must be a multiple of 32. Transport is
a uint32 little-endian JSON-header length, four-byte-padded UTF-8 JSON, then
float32 little-endian data shaped `[time,pair,frequency,component]`. Complex
components are Real/Imag; preview components are mean Real/Imag/|V| so phase
cancellation cannot erase mean magnitude. The browser validates the sequence
ID, timestamps, pairs, shape and exact byte length before showing a chunk.

Reads reuse VisStore and casm_io packet-to-baseline mapping. Server cold reads
are serialized, with a 256 MiB complex-chunk cache and eight small manifests.
The browser has a 384 MiB numeric-buffer budget, current-window buffering and
two-chunk look-ahead, with two requests at a time. Coloured canvas strips are
cached separately, retaining at most two quantity/direction variants per
baseline/chunk. Very large required windows fail visibly; total process memory
also includes arrays, rendering and library overhead beyond the cache budgets.
There are no per-frame PNG requests, new persistent products or hardware calls.

Tests: `tests/test_visibility_playback.py`,
`frontend/tests/visibility-playback.test.mjs`, and the saved-data-only
`scripts/check_visibility_playback_browser.py`. Browser cadence checks measure
frame commits, not physical display refresh. This validates presentation and
arithmetic, not array health or astrophysical interpretation.

## Acceptance check, 2026-09-30

The local saved-data browser check covered the 16-input default, all quantities,
SNAP/station order, reference changes without moving the cursor, 136 upper-triangle
cells, rolling averages, day/48-hour/week selection, historical dates, looping,
PNG downloads and an injected load failure followed by retry. No page errors or
non-GET API requests occurred. At 4×, 18 consecutive reference-cross dynamic frames
crossed a chunk boundary without skipping; commit intervals were 219–280 ms.
This is a local-browser sample, not a network-speed or all-configurations guarantee.

A separate cold 16-auto benchmark took 5.6 s to prepare a 24-hour sequence plus
24-hour context (629 integrations each). Its process peak RSS was 318 MiB.
Cold matrix/week selections read more data and can take longer to prepare;
playback shows buffering until the required window is available.

Browser evidence and downloaded figures are in
`/home/casm/scratch/casm-observation-preview/visibility-playback-check/`.
The saved `result.json` records timings and checks; `cross-dynamic.png` is the
actual application screenshot. The central docs build, local links and exports
pass. Its source-snapshot drift check still reports unrelated changes in
casm_io, casm_vis_analysis and casm_calibrator; those snapshots were not refreshed
as part of this monitor change.
