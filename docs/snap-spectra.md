# Full-band SNAP spectra

Open **SNAPs** at `/snaps`. The white panels show **Power (dB)** versus
**Frequency (MHz)** across the full **4096-channel, 375–500 MHz** board band.
These are stored autocorrelator readouts, not the 3072-channel production/Kafka
spectra. Opening the page or browsing history never contacts hardware.

## Views and scales

- **Compact · SNAP order** is the default, with all currently wired stations.
  On September 24, 2026, this is 24 stations; it is not the 17-antenna visibility
  inspection preset or the 16-antenna calibration set.
- **Compact · station order** packs panels by plank. **Station grid** preserves
  the six east–west positions, including empty and hidden positions. Entirely
  empty plank rows are compressed. The nearby layout key toggles stations.
- **All 12 ADCs per SNAP** includes unwired inputs: 48 panels for four boards.
  Station arrangements place unwired inputs in their own SNAP-ordered section.
  Relay boards have no antenna spectra and do not receive fabricated panels.
- Labels show station, antenna, SNAP, slot and ADC from the current layout/map.
  Historical comparisons follow board IP and ADC, not historical station wiring.
- Spectra retain all native channels with no rebinning or normalization.
  The existing board frequency convention is descending 500 to 375 MHz in the
  API; the plot reads left-to-right from 375 to 500 MHz.
- Power is `10 log10(native linear power)`, relative to one native power unit,
  **not dBm** or calibrated flux. The existing hardware getter divides out its
  accumulation length before storage. Zero/nonfinite/negative channel values
  are not clipped to an invented noise floor: the line breaks and counts are
  displayed. Zero power has no finite dB value.
- A shared −100 to 0 dB spectrum scale stays fixed across panels and dates.
  Change its limits or explicitly **Fit current spectra**. Clipped points are
  counted in each panel. No automatic per-panel rescaling hides level changes.

Click any spectrum to expand it and see full-band power versus time. The trend
is `10 log10(mean(linear power))` over all 4096 channels, including true zeros.
An incomplete band gives a missing point, not a mean over a changing channel
set. The optional focused trend scale reveals drift in absolute dB. Lines break
at gaps longer than 1.5 configured read intervals and at EQ/FFT epoch changes.
Only saved samples are plotted. A single point is still visible.

## History and acquisition

**Latest spectra** opens by default and refreshes saved data every minute.
Acquisition status polls every ten seconds. **History** selects actual saved
snapshots over the last 1–365 days (default 30), with an optional orange dashed
overlay of the first snapshot in that interval. The slider steps through
available reads, not equal wall-clock intervals. Per-board timestamps remain
visible. A snapshot groups a sequential board pass within ten minutes; it is
not a simultaneous measurement. An historical board with no saved spectrum
within 1.5 read intervals before the selected time is missing, not held forward.

The latest view can show an older successful spectrum after a failed attempt;
its stale timestamp and the latest attempt's errors remain visible separately.
An absent/corrupt shard does not become zero signal. EQ/FFT epoch differences
between reference and selected spectra are flagged. Unrecorded analogue gain
changes cannot be identified from these tags.

**Ping now · get spectra** explicitly submits the existing `snap_read` job
through the protected loopback bridge. It is a diagnostic read, not an ICMP
ping. The existing lease, sequential board reads, timeouts and five-minute
manual cooldown remain authoritative. No new hardware reader is introduced.
The getter selects the autocorrelation diagnostic mux and arms its readout.
It does **not** program firmware, change EQ/gains, issue a PPS synchronization,
change weights, or alter the production observing pipeline.

The requested source profile is hourly (`snap.read_interval_s: 3600`).
**Rollout is pending:** the running monitoring collector still imports the
older locked worktree with a 7200-second profile and the shared-liveness-slot
starvation bug. The tested independent scheduling-slot implementation exists
in main. Activation requires updating/restarting **only the monitoring
collector**, separately from this preview. No running production service was
changed during this UI task. A profile interval is not proof of acquisition;
read the actual timestamps. No preview scheduler or duplicate worker is added.

## Storage and API

The existing collector stores linear float32 arrays in
`/mnt/nvme3/casm_monitor/shards/snap_read/`, indexed by the `shards` table in
`/mnt/nvme3/casm_monitor/monitor.sqlite`. `snap_read_latest` records each latest
attempt and its errors. This stream has **no expiry**. Four 12×4096 float32
spectra per hour cost about **18 MiB/day (6.4 GiB/year)** before compression and
metadata. History uses this storage, not zapdos, Kafka or browser state.

New read-only routes, with Unix-second timestamps:

| Route | Result |
| --- | --- |
| `GET /api/snap-workspace/spectra[?at=<unix>]` | Current layout, shared frequency axis, per-board spectra in dB, acquisition time, stale flag, errors and zero/invalid channel counts. `at` omitted means latest saved success. |
| `GET /api/snap-workspace/spectra-catalog?days=30` | Bounded saved-read timeline and board coverage; no array data. |
| `GET /api/snap-workspace/spectra-trend?ip=<configured-ip>&adc=0&days=30` | Full-band mean power, real timestamps, EQ/FFT epoch tags and missing samples. |

Catalog queries stop at 20,000 records. Trends preflight shapes, dtypes and
path containment, with a 512 MiB decoded read budget; shorten the period if
it exceeds the budget. An eight-selection in-memory cache avoids repeated
trend reads. Only one cold trend is loaded at a time; no plot artifacts are
written. A requested year can exceed the read budget once enough data exist.
The existing `/antennas` page remains available as **Transmitted-band history**;
its old Kafka history is separate from these full-band snapshots.

## Checks

`tests/test_snap_spectra.py` covers native channel order/shape, mapping, linear
averaging, zero/missing values, gaps, bounds, read budgets, lost shards and
read-only access. Existing acquisition/scheduler tests use fake hardware.
`scripts/check_snap_spectra_browser.py` checks current saved data, all layouts,
48-ADC mode, overlays, expansion and 320–1500 px widths. Its acquisition POST
is intercepted: it never causes a hardware read. Real screenshots are under
`docs/screenshots/2026-09-24/snaps-*.png`.
