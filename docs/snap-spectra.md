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
  The count is data-driven and matches the default in Visibilities and Overview.
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
  antenna/date. The reference selector is removed. Negative original values
  are expected; neither scale is **dBm** or calibrated flux. The getter divides out its
  accumulation length before storage. Zero/nonfinite/negative channel values
  are not clipped to an invented noise floor: the line breaks and counts are
  displayed. Zero power has no finite dB value.
- All panels share one power scale spanning **every finite displayed channel**,
  with padding. There is no percentile clipping. Limits update with each saved
  spectrum or selection change, including all-ADC and individually shown inputs.
  Expanded spectra use the same limits. Scale/refit controls are removed.
  This supersedes the earlier pinned percentile scale and its orange clipping
  markers; data values and the fixed dB reference are unchanged.

Click any spectrum to expand it and see full-band power versus time. The trend
is `10 log10(mean(linear power))` over all 4096 channels, including true zeros.
An incomplete band gives a missing point, not a mean over a changing channel
set. The automatically focused trend scale reveals drift in absolute dB. Lines break
at gaps longer than 1.5 configured read intervals and at EQ/FFT epoch changes.
Only saved samples are plotted. A single point is still visible.

The September 25 read-only direction audit compared saved SNAP spectra with
visibility autos 21–26 seconds away. Noncentral features at 400.665 and
463.715 MHz aligned in the current mapping; the exact alternative
`375+k*125/4096` fit worse. No axis reversal was made. Evidence and limitations:
`casm-wiki/evidence/2026-09-25/snap-control-frequency-followup.md`.

## History and acquisition

The latest spectra refresh from saved data every minute; acquisition status
polls every ten seconds. History/overlay selectors are removed from the page.
Click a spectrum for its saved 30-day power trend. The history API remains
available, including dated spectra and catalogue queries. Per-board timestamps
remain visible: a board pass is sequential, not a simultaneous measurement.

The latest view can show an older successful spectrum after a failed attempt;
its stale timestamp and the latest attempt's errors remain visible separately.
An absent/corrupt shard does not become zero signal. EQ/FFT epoch differences
between reference and selected spectra are flagged. Unrecorded analogue gain
changes cannot be identified from these tags.

The preview's fresh history begins at **2026-09-25 05:12:31 UTC**, the selected
all-four successful read. Earlier shards and database rows are retained, not
deleted. `snap_history_epoch.json` under `observation_cache_root` holds a
version-1 `start` timestamp; missing/invalid configuration leaves all history
available. `archive=true` on spectra/catalog/trend includes earlier records.
The default trend and catalogue queries use the same epoch.

## Live status

Separate **Streaming** and **PPS timing** boxes appear per board and in
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
The strict `pps_status` and `pps` aggregate retain the exact-match rule.
Displayed `timing_status` / `timing` instead compare against an explicitly
accepted fixed reference. On September 25 at 06:52:43 UTC the operator-requested
reference was saved from the fresh 06:38:49 read: SNAPs 0/1/2/3 have offsets
**0/−1/0/−1 ticks** relative to SNAP 0. Green means advancing PPS with unchanged
offsets, not sample-exact alignment. Changed offsets/reference or stopped PPS
are red **Needs attention**. Missing, failed or stale checks remain Unknown.
Checks expire after 90 minutes; newer failures supersede older success.
The summary requires every configured antenna board and does not grade relays.
No source-check age or periodic native visibility analysis affects these boxes.
The separate dated science result remains in `casm-wiki/monitor-snap-source-check.md`.
The baseline is stored in monitoring SQLite at `watermarks(snap_read, pps_baseline)`.
Acquisitions never relearn it. Only after explicit operator acceptance, replace
it using a fresh saved complete read (no hardware contact from this command):

```bash
PYTHONPATH=. /home/casm/software/dev/casm_venvs/casm_offline_env/bin/python \
  scripts/check_snap_timing.py --accept-saved-baseline
```

Use `CASM_MONITOR_CONFIG` for the shared monitor config when outside its service
environment. Unreadable, stalled, inconsistent or stale evidence is rejected.
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

The standalone command and hourly spectrum job now share the persisted read
lease. Every all-antenna spectrum acquisition runs the same bounded getter-only
PPS comparison afterwards, including Ping now. Partial-board requests never
contact additional boards. Its newest attempt is stored in monitoring SQLite
as `watermarks(snap_read, pps_timing)`; failures publish Unknown, not old green.
The CLI also writes the requested local JSON. The API chooses the newest saved
attempt and retains the 90-minute expiry. GET/page refresh performs no hardware
read. No source re-check runs in this job.
Activated in the locked monitoring checkout on September 25 at 06:38 UTC;
a fresh getter-only verification succeeded for all four boards. Only the 8061
preview restarted to consume the new evidence source; the worker/collector and
observing services were not restarted. Subsequent job subprocesses load the fix.
The first subsequently observed automatic job, **863**, finished at September
25 **07:14:51 UTC**, with fresh spectra and PPS evidence for all four antenna
boards; accepted-offset timing remained 4/4 OK. The monitoring DB job result
and `snap_read/pps_timing` watermark retain the measurements.
The September 25 management lockout was released by a separately approved
operator session at 04:41 UTC, completing stale TFTP transfers without reflash
or re-sync. The saved 04:43 UTC check found advancing PPS on all four boards,
SNAP 2 equal to SNAP 0 and stable −1 TT tick on SNAPs 1/3. Its onset is unknown;
there is no basis to assume it already existed during the September 17 solve.
Procedure and dated history: `casm-wiki/snap-control-path.md` and
`casm-wiki/evidence/2026-09-25/snap-control-frequency-followup.md`.

**Ping now · get spectra** explicitly submits the existing `snap_read` job
through the protected loopback bridge. It is a diagnostic read, not an ICMP
ping. The existing lease, sequential board reads, timeouts and five-minute
manual cooldown remain authoritative. No new hardware reader is introduced.
The getter selects the autocorrelation diagnostic mux and arms its readout.
It does **not** program firmware, change EQ/gains, issue a PPS synchronization,
change weights, or alter the production observing pipeline.

Hourly scheduling was activated on September 25 at 05:30 UTC, by updating the
locked monitoring checkout's `snap.read_interval_s` to 3600 and applying the
independent SNAP scheduling slot. Only `casm-monitor-collect.service` restarted;
the existing serialized job worker and observing pipeline were unchanged.
Both completed-read and scheduling-slot guards prevent duplicate hourly reads;
connectivity probes cannot starve spectra. A manual read postpones the next
automatic read until one hour after its completion. The collector checks every
minute, so queueing can follow the due time by up to a minute, plus worker delay.
The five mocked scheduling regressions passed against the running checkout.
Read actual acquisition timestamps to verify long-term cadence. No preview
scheduler, duplicate hardware reader or firmware change was introduced.

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
is removed. SNAPs shows latest spectra and expanded trends, without duplicate
Visibilities navigation or a second transmitted-band history presentation.

## Checks

`tests/test_snap_spectra.py` covers native channel order/shape, mapping, linear
averaging, zero/missing values, gaps, bounds, read budgets, lost shards and
read-only access. Existing acquisition/scheduler tests use fake hardware.
`scripts/check_snap_spectra_browser.py` checks current saved data, all layouts,
48-ADC mode, expansion, shared full-data limits (including new extreme values
arriving during an already-open page's saved-data refresh), removed controls,
separate status boxes and 320–1500 px widths. Its acquisition POST
is intercepted: it never causes a hardware read. Real screenshots are under
`docs/screenshots/2026-09-25/snaps-*.png` (UTC capture date).

`scripts/audit_snap_spectra.py` compares API values with at most four saved
shards per board. The September 25 audit confirmed the 489–494 MHz features
on N11/N16 and the 493.682861 MHz peak in the native arrays, including September
9 reads. This verifies data-to-display fidelity, not an RFI or hardware cause.
N11's displayed successful read was September 12, unlike N16's September 25
read; compare their labels before interpreting them as simultaneous.
