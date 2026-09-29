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
The browser keeps at most eight response objects in memory, not the entire
archive. Today's catalogue refreshes; the chosen spectrum remains pinned.

## Validation

`tests/test_snap_changes.py` injects gain steps, broad shape changes, single
channel peaks (including repeated/intermittent lines), zeros, NaNs, missing reads, rapid repeat reads, configuration
changes and normally variable baselines. API tests cover exact ID validation,
archive dates, missing boards, anchored trends and no jobs/watermark writes.

`scripts/check_snap_history_browser.py` exercises saved dates, comparisons,
trend-point navigation, badge expansion, downloads and phone widths against the
local preview; it blocks every non-GET API request.
`scripts/audit_snap_changes.py --output <private-path> --backtest-hours 24`
checks stored arrays only. Run with `PYTHONPATH=.` in this checkout and the
offline Python environment. This is a bounded retrospective threshold check,
not proof that flags correspond to hardware faults. Dated results are in
`casm-wiki/monitor-snap-history.md`.
