# Saved SNAP history and change checks

The SNAPs page defaults to Live. History selects an exact saved board pass by
UTC date and a within-day time slider. Day arrows skip to dates with saved data;
read arrows step through saved reads, including across midnight. Selecting a day
keeps the nearest available time of day. Empty dates explicitly show no data.
**Compare with…** opens an optional second navigator, initially the preceding
read relative to the selection (not an unrelated recent date). Close comparison
removes its orange dashed trace. Missing boards
are blank, and archive age alone is not a stale-read warning. Click a full-band
power trend point to open its saved spectrum. Zoom and PNG download remain
available. Station and beamforming labels always describe the current mapping.

## Playback

In History, **Play day** opens the selected inputs together in a full-window
grid. Live remains the page default; opening History alone does not play.
The default interval is the selected UTC day. **Last 48 hours** and **Last week**
end now when today's date is selected, or at the selected past UTC day's end.
The date selector covers all retained dates. The clock switches between UTC and
OVRO local time, including the date and PDT/PST abbreviation.

Play/pause, previous/next read, the time slider, 0.5/1/2/4× speed and optional
Loop control saved-read playback. At 1× each read gets one second; 4× targets
250 ms. Playback stops at the end unless Loop is enabled and pauses when the
browser tab is hidden, a request fails or a plot is enlarged. **Open this read**
returns to the ordinary History view at that exact snapshot. Closing leaves
the previous selection unchanged. The zoom viewer retains PNG download.

Frequency and power axes stay fixed throughout an interval. Power limits cover
every finite native channel of the selected inputs across that interval, with
padding; no percentile clipping or per-frame normalization. The player fits a
compact grid to the viewport; dense/all-ADC and small-screen views scroll inside
the grid rather than making labels illegible. The usual History/zoom spectrum
proportions are unchanged.

Reads are pinned when playback opens. Each frame groups a sequential board pass,
not simultaneous acquisitions. Missing boards remain blank, gaps are labelled,
and timestamps always describe the displayed frame, including during buffering.
EQ/FFT changes are labelled against the first setting encountered. No samples
or missing hours are interpolated. Selection, wiring and beamforming membership
are captured from the page when playback opens, not reconstructed historically.

The browser fetches up to 12 frames at a time, only for displayed inputs, and
prefetches the next batch. It retains two batches plus the displayed frame while
seeking, not the full week. Steps within a loaded batch make no HTTP request.
Every spectrum still contains 4096 separate channels; float32 transport replaces
large JSON arrays, not the scientific data's frequency resolution. Initial
buffering or an unusually slow connection can still delay playback; no reads are
silently skipped. Failed batches can be retried. Closing cancels pending frame fetches.

## Change badges

The Latest spectrum check covers all currently wired inputs, independently of which
plots are shown. It refreshes from stored data every minute while visible.
It does not acquire hardware data, send notifications or keep an alert ledger.
The hourly spectrum/PPS job is unchanged.

- **No large power / shape change:** below the broad-change thresholds, not proof
  of health or a claim that every channel is unchanged.
- **Fine-channel variation:** collapsed neutral detail, with affected channel
  count/fraction and band-power change. Never amber/red solely for narrow peaks.
- **Amber:** a band-power, broad-shape or new-zero threshold exceeded.
- **Red / Repeated:** the same large change in two reads separated by
  0.5–1.5 configured intervals (30–90 minutes at hourly cadence).
- **Unknown:** missing, overdue, failed or incomplete data, unknown EQ/FFT
  identity, insufficient history, or a bounded-read limit. Never green.
- **EQ / FFT changed:** recorded configuration differs from the previous read.

The newest two reads are compared with the preceding 24-hour baseline, excluding
both tested reads. Require at least six valid same-EQ/FFT reads spanning four
hours, each with at least 80% positive channels. Channel references are medians
in dB with majority support. Thresholds include normal historical scatter
(1.4826 × median absolute deviation):

| Change | Minimum threshold | Confirmation |
| --- | --- | --- |
| Full-band power | 6 dB or 6 × historical scatter, whichever is larger | Same direction |
| Broad spectral shape | 3 dB or 6 × per-channel scatter on at least 10% of channels | At least 5% of all channels overlap in the same direction |
| Fine-channel excursion (information only) | 12 dB or 8 × per-channel scatter | Never promoted to a red alarm on its own |
| New zero-power channels | At least 1% of the band with a supported positive baseline | At least 1% overlap |

Band power averages native linear power before converting to dB, including real
zeros. Shape/peak checks subtract each read's median spectral offset to separate
shape changes from uniform gain shifts. Displayed spectra are **not** normalized.
Fine-channel details report frequency, direct difference against the plotted
reference and how often that channel exceeded the current shape threshold in
baseline reads. No novelty or physical-cause claim is made. An all-zero
read is signal loss; NaN/negative/incomplete reads are unknown.

Clicking an entry opens a separate evidence window without changing the date,
selection or manual comparison behind it. Orange is the **exact per-channel
median used by the check**, labelled with its read count and actual time span.
A second plot shows selected minus reference in dB; vertical markers identify
reported fine-channel excursions. The table's dB values match those traces.
The uniform offset removed for the shape test is stated separately. Full-band
power compares each linear-band mean with the median of baseline band means;
it is not the mean of the orange median-channel spectrum.

The check's sample IDs are pinned, so later acquisitions cannot move an open
comparison. `GET /api/snap-workspace/change-detail?ids=<sample_ids>&adc=<adc>`
reproduces its bounded, single-board input set and returns the plotted arrays.
Missing stored evidence makes that comparison unavailable.

Version 1 labelled a single high channel "New strong spectral peak" and could
turn it red after two reads. It did not test novelty, and its representative
orange read was not the mathematical reference. Those presentation/severity
claims were withdrawn on September 29 after native-array and independent review.
Version 2 retains the measurements as neutral details and uses the exact
reference. The power, broad-shape and zero thresholds were not raised to hide
data. Intermittent lines can be above a median repeatedly without being new.

This is a relative-change diagnostic, not a calibrated hardware-fault classifier.
Sky variation, RFI and unrecorded analogue gain changes can trigger it. Existing
faults can be stable; a persistent change can become part of the rolling
baseline. Missing attempts are inferred from the latest saved failure or overdue
timestamp, not invented as zero spectra. No offsets or hardware are corrected.

## Storage and budgets

History reuses `snap_read` Zarr arrays and the SQLite manifest documented in
[SNAP spectra](snap-spectra.md#storage-and-api). No second spectrum database or
retention policy is added. Thirty days limits the displayed power trend, not
retention; the date selector lists every retained day.

The change endpoint scans at most 28 hours and 128 reads per board, loading one
board at a time. Extra reads produce Unknown. Small summary caches expire after
120 seconds and hold four entries. Existing path/shape checks still apply.
Trend reads retain their 512 MiB budget and eight small summary-cache entries.
The ordinary history browser keeps at most eight response objects in memory,
not the entire archive. Today's catalogue refreshes; the chosen spectrum remains
pinned. Playback separately keeps the bounded two-batch buffer described above.
Its fixed-axis manifest reads at most 1024 board shards within 512 MiB, one board
at a time, and caches four small extrema summaries. Each packed batch is limited
to 12 frames, 48 selected inputs and 64 MiB of raw reads. No movie files or new
server-side spectrum cache are written.

## Validation

`tests/test_snap_changes.py` injects gain steps, broad shape changes, single
channel peaks (including repeated/intermittent lines), zeros, NaNs, missing reads, rapid repeat reads, configuration
changes and normally variable baselines. API tests cover exact ID validation,
archive dates, missing boards, anchored trends and no jobs/watermark writes.

`scripts/check_snap_history_browser.py` exercises saved dates, comparisons,
trend-point navigation, badge expansion, downloads and phone widths against the
local preview; it blocks every non-GET API request.
`scripts/check_snap_playback_browser.py` checks the player, fixed axes, intervals,
seeking and 4× progression across a batch boundary. `--dom-only` explicitly
excludes native painting, hit-target and PNG-download validation.
`scripts/measure_snap_playback.py --output <private-path>` measures saved-data
manifest/batch requests and the web service's CPU/RAM without contacting hardware.
`scripts/audit_snap_changes.py --output <private-path> --backtest-hours 24`
checks stored arrays only. Run with `PYTHONPATH=.` in this checkout and the
offline Python environment. This is a bounded retrospective threshold check,
not proof that flags correspond to hardware faults. Dated results are in
`casm-wiki/monitor-snap-history.md`.
