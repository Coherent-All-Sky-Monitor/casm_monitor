# Saved SNAP history and change checks

The SNAPs page defaults to Live. History selects an exact saved board pass by
UTC date/time; Previous/Next crosses date boundaries. Compare spectra adds an
orange dashed reference to the blue spectrum on the same axes. Missing boards
are blank, and archive age alone is not a stale-read warning. Click a full-band
power trend point to open its saved spectrum. Zoom and PNG download remain
available. Station and beamforming labels always describe the current mapping.

## Change badges

The Changes strip covers all currently wired inputs, independently of which
plots are shown. It refreshes from stored data every minute while visible.
It does not acquire hardware data, send notifications or keep an alert ledger.
The hourly spectrum/PPS job is unchanged.

- **Stable vs history:** below these change thresholds, not proof of health.
- **Watch** (amber): a large change in the latest read.
- **Needs attention** (red): the same change in two reads separated by
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
| Strong new peak | 12 dB or 8 × per-channel scatter | At least one channel overlaps |
| New zero-power channels | At least 1% of the band with a supported positive baseline | At least 1% overlap |

Band power averages native linear power before converting to dB, including real
zeros. Shape/peak checks subtract each read's median spectral offset to separate
shape changes from uniform gain shifts. Displayed spectra are **not** normalized.
Peak badges report frequency and excess above that shape reference. An all-zero
read is signal loss; NaN/negative/incomplete reads are unknown.

Clicking a badge compares the current read with an actual baseline read whose
band power is closest to the historical median. The orange trace is that saved
read, **not** the per-channel median used by the detector.

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
channel peaks, zeros, NaNs, missing reads, rapid repeat reads, configuration
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
