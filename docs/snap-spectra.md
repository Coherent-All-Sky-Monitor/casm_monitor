# Full-band SNAP spectra

Open **SNAPs** at `/snaps`. The white panels show **Power (dB)** versus
**Frequency (MHz)** across the full **4096-channel, 375–500 MHz** board band.
These are stored autocorrelator readouts, not the 3072-channel production/Kafka
spectra. Opening the page or browsing history never contacts hardware.

## Views and scales

- **Compact · SNAP order** defaults to the latest recorded deployed CB weights,
  inspected across every channel, polarization and beam. September 25's product
  `7f00805b81f13763` has 16 populated antenna inputs, out of 24 wired inputs.
  **All wired** and **All 12 ADCs per SNAP** expose the other inputs.
  The count is data-driven, not the separate Visibilities inspection preset.
- **Compact · station order** packs panels by plank. **Station grid** preserves
  the six east–west positions, including empty and hidden positions. Entirely
  empty plank rows are compressed. The nearby layout key toggles stations.
- **All 12 ADCs per SNAP** includes unwired inputs: 48 panels for four boards.
  Station arrangements place unwired inputs in their own SNAP-ordered section.
  Relay boards have no antenna spectra and do not receive fabricated panels.
- Labels show station, antenna, SNAP, slot and ADC from the current layout/map.
  Status boxes and expandable control-read summaries also show the board IP.
  Historical comparisons follow board IP and ADC, not historical station wiring.
- Green borders and **Beamforming** badges mark the inspected deployed union,
  including in all-ADC view. The nearby layout and Overview map show every wired
  station: green/BF for deployed members, blue for other wired inputs, and × for
  absent antennas. Hiding a spectrum does not change beamforming membership.
  History keeps current membership explicitly labelled, not historical membership.
  Missing/mismatched deployment evidence or changed slot identities give Unknown,
  not a fallback to `functional` or layout `include_in_beamforming` flags.
- Spectra retain all native channels with no rebinning or normalization.
  Native channel centres are `500 - k*125/4096` MHz, descending: 500 through
  375.030518 MHz. The shared plot axis runs **374.9–500.1 MHz** left-to-right,
  leaving room at both edges without inventing samples outside the band.
- Stored/API power is `10 log10(native linear power)`. The default display
  reference is **10⁻¹⁰ native power units**, a constant +100 dB offset for every
  antenna/date. Original dB re 1 remains selectable. Negative original values
  are expected; neither scale is **dBm** or calibrated flux. The getter divides out its
  accumulation length before storage. Zero/nonfinite/negative channel values
  are not clipped to an invented noise floor: the line breaks and counts are
  displayed. Zero power has no finite dB value.
- All panels share one power scale, pinned to the initially loaded snapshot
  until **Refit scales**. **Bandpass detail** fits the 1st–99th percentiles of
  all displayed-set channels, with padding. **Full spectrum** includes the
  extrema; **Custom shared limits** allows explicit bounds. Changing to all
  ADCs includes those inputs in the shared fit; hiding panels does not refit.
  Orange edge markers and counts identify clipped peaks. The data are unchanged.

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

## Live status

Separate **Streaming** and **PPS alignment** boxes appear per board and in
Overview. They refresh saved evidence every 30 seconds, independently of the
selected historical spectrum. Green streaming **OK** means all wired inputs
have nonzero cached visibility data no older than 15 minutes. This is timestamped
downstream evidence, not a live transmit-counter measurement or antenna-quality
assessment. Old/missing/zero input data must not turn green.

Beamforming selection uses `/api/snap-workspace/beamforming`, polled every 30 s.
The cached nonzero-weight inspection must match the product, path and file
identity associated with all six streams' latest registry events. This is
recorded deployment evidence, not new runtime payload readback. GETs do not open
the large weights payload. The existing figure worker refreshes its membership
cache; an explicit offline preview-only refresh is also available:

```bash
PYTHONPATH=. /home/casm/software/dev/casm_venvs/casm_offline_env/bin/python \
  scripts/check_beam_membership.py \
  --output-root /home/casm/scratch/casm-observation-preview
```

This reads at most 400 MiB in chunks and writes only a small `membership.json`.
It does not generate/deploy weights, alter the layout CSV or contact hardware.
A new unmatched deployment stays Unknown until its payload inspection exists.

PPS period checks use the existing 0.1% tolerance around the board clock.
Period/count snapshots alone cannot prove cross-board sample alignment or
continued pulse arrival. The explicit read-only check below records two stable
common-edge telescope-time frames, advancing counts/TT and exact deltas to SNAP 0.
Verified boards turn green; failed reads stay Unknown. A measured offset or
stalled PPS is Needs attention. Checks expire after 90 minutes, and later failed
PPS attempts supersede old success. The array summary is green only if every
configured antenna board is verified. It does not grade relay-board telemetry.
Failed management reads leave firmware/PPS state unknown, even when the old
driver recorded `programmed=False`. The corrected reader queries the transport
inventory directly; its worker rollout remains pending with the collector.

The existing spectrum-reading recipe is in `casm-wiki/antenna-health-triage.md`:
`snap_ops/plot_snap_autocorrs.py` wraps the established zapdos reader. Reading
spectra needs running firmware, an ADC clock and accumulating autocorrelator,
not active UDP transmission or cross-board synchronization. Do not initialize,
program or re-sync a board to troubleshoot monitoring reads.

### Explicit PPS-only check

From the monitor checkout on corr1:

```bash
PYTHONPATH=. /home/casm/software/dev/casm_venvs/casm_offline_env/bin/python \
  scripts/check_snap_timing.py \
  --output /home/casm/scratch/casm-observation-preview/snap_timing/latest.json
```

This bounded adapter uses the existing `period_pps()` / `get_tt_of_pps(False)`
getters from the installed driver, as used by `multi_snap_config.verify()`.
Direct count reads prevent the driver's swallowed errors from becoming zero.
Reference reads bracket each frame to reject edge crossings; a second frame
excludes identical-but-frozen counters. Per-board count totals may differ with
uptime; matching TT and advancing counts are required, not equal count totals.
Telescope-time integers are saved as strings to preserve all 64 bits in JSON.
The adapter does not execute the configuration CLI or its `--sync-only` flag:
that flag **changes synchronization**. The older
`ssh zapdos 'python3 /home/user/pps_status.py 10'` checks pulse arrival/rate,
not sample-exact cross-board alignment, and its timing-chain caption is outdated.

Only local preview evidence is written; GET/page refresh performs no hardware
read. The PPS-only check is manual, not a new scheduler. Hourly collector
activation remains a separate rollout. On September 25 02:43:53 UTC, SNAPs 0/2
matched to zero ticks over advancing PPS; SNAPs 1/3 had unreadable control
registers. No firmware, EQ, gains, synchronization or observing state changed.
At 03:01 UTC, direct read-only TFTP packets exposed the actual .51/.73 error:
**Only one connection at a time is supported**. The Python TFTP library hides
that message behind “Access violation”. Canonical 0→2→3→1 ping/read order did
not change the failure. An active owner versus stale firmware session remains
unresolved; no session was aborted or recovered. Evidence is recorded in
`casm-wiki/evidence/2026-09-25/snap-monitor-checks.md`.

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
| `GET /api/snap-workspace/health` | Small per-board and aggregate live-status response from existing cached visibilities and latest stored board attempts. No spectrum-shard loading or hardware contact. |
| `GET /api/snap-workspace/spectra[?at=<unix>]` | Current layout, shared frequency axis, per-board spectra in dB, acquisition time, stale flag, errors and zero/invalid channel counts. `at` omitted means latest saved success. |
| `GET /api/snap-workspace/spectra-catalog?days=30` | Bounded saved-read timeline and board coverage; no array data. |
| `GET /api/snap-workspace/spectra-trend?ip=<configured-ip>&adc=0&days=30` | Full-band mean power, real timestamps, EQ/FFT epoch tags and missing samples. |

Catalog queries stop at 20,000 records. Trends preflight shapes, dtypes and
path containment, with a 512 MiB decoded read budget; shorten the period if
it exceeds the budget. An eight-selection in-memory cache avoids repeated
trend reads. Only one cold trend is loaded at a time; no plot artifacts are
written. A requested year can exceed the read budget once enough data exist.
The legacy `/antennas` URL remains available for old bookmarks, but its submenu
is removed. SNAPs exposes only its own Latest/History controls, without duplicate
Visibilities navigation or a second transmitted-band history presentation.

## Checks

`tests/test_snap_spectra.py` covers native channel order/shape, mapping, linear
averaging, zero/missing values, gaps, bounds, read budgets, lost shards and
read-only access. Existing acquisition/scheduler tests use fake hardware.
`scripts/check_snap_spectra_browser.py` checks current saved data, all layouts,
48-ADC mode, overlays, expansion, shared limits, reference-offset invariance,
separate status boxes and 320–1500 px widths. Its acquisition POST
is intercepted: it never causes a hardware read. Real screenshots are under
`docs/screenshots/2026-09-25/snaps-*.png` (UTC capture date).

`scripts/audit_snap_spectra.py` compares API values with at most four saved
shards per board. The September 25 audit confirmed the 489–494 MHz features
on N11/N16 and the 493.682861 MHz peak in the native arrays, including September
9 reads. This verifies data-to-display fidelity, not an RFI or hardware cause.
N11's displayed successful read was September 12, unlike N16's September 25
read; compare their labels before interpreting them as simultaneous.
