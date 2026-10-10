# Webpage resources and data products

## Independent acquisition and retired imaging, October 10, 2026

Hourly and manual SNAP diagnostics run in `casm-monitor-snap-read.service`,
using `python -m casm_monitor.jobs.worker --kinds snap_read`. It claims only
SNAP jobs from the existing production database. `casm-monitor-jobs.service`
now claims only `render_figures`; an older queued render cannot delay a SNAP
read. The separate calibration store and worker are unchanged.

Imaging has been removed from the scheduled targets and job dispatch. Old
queued requests containing `imaging` record it as skipped and still process
their supported targets. Imaging code and saved products remain on disk for
future development; no imaging is restarted by enabling the monitor workers.

The remaining render pool is two threads, with a 16 GiB service memory limit,
four-CPU quota and low CPU/I/O priority. The SNAP worker has a separate 2 GiB,
one-CPU budget. These bounds reduce competition; they do not guarantee latency
under every shared-host workload. Collector and worker units explicitly use
the main checkout as their working directory, avoiding the old editable-install
worktree. Current unit templates are in `deploy/systemd/`.

The existing hourly scheduler, five-minute manual cooldown, persisted hardware
read lease and sequential board acquisition are retained. Spectra and the
getter-only PPS comparison run in the same SNAP job. Diagnostic acquisition
uses the existing autocorrelator mux/readout controls; no FPGA programming, EQ,
PPS re-sync or weights deployment is added. Opening a page remains a saved-data
read. Install all three collector/render/SNAP units together, reload systemd,
restart the collector, and enable/start both workers. Do not restore the old
unfiltered jobs unit or launch a second unrestricted worker on this database.

Queue verification: inspect `jobs.kind`, `state`, `worker`, `params` and `result`
in `/mnt/nvme3/casm_monitor/monitor.sqlite`, plus each service's journal.
`job_worker_started` records the allowed kinds. A completed job's per-board
errors and actual saved-spectrum timestamps remain authoritative; `done` alone
does not establish that every board read succeeded. Tests cover mixed-kind FIFO,
exclusive claims, scoped reconciliation, legacy imaging requests, scheduler
cooldowns and the manual acquisition route with mocked boards.

## SSH-focused navigation, October 6, 2026

Overview, Search, Visibilities and SNAPs restore cached plots and selected
controls when switching tabs. The cache is session memory, cleared on a full
reload: at most 48 responses and a 64 MiB serialized-text size estimate, not a
cap on total browser heap. Existing movie buffers and Source history caching
remain separate. Identical in-flight GETs share one request. Departed pages
stop polling; already-running shared reads may finish and populate the cache.
No speculative native visibility reads or hardware queries run on navigation.

As of October 8, live health revalidates every 30 s and can retain the last
successful response for at most 120 s while waiting. Polling no longer discards
it at the start of a request. Actual measurement-age rules still apply, failures
show Unknown immediately, and a local deadline expires a hanging response.
Snapshot plots retain actual data timestamps and refresh failures.
Newly selected intervals/quantities never inherit a different result's labels.
Overview and Search keep successful rolling plots visible during updates, with
their displayed interval. Rolling endpoints persist across quick tab returns,
then advance at the existing two-/five-minute cadence. Search loads its small
live stream-status request independently of the full figure; empty-gulp fractions
come from the separately dated plot snapshot. The T1 server cache is an eight-entry
LRU lasting five minutes; concurrent requests reuse a single plot render.

Text responses use gzip level 3; existing gzip payloads, PNG/JPEGs, archives,
range responses and event streams are not recompressed. Fingerprinted assets
and completed scientific/Search plot products use immutable HTTP caching.
Acquisition/upload actions still fetch their own current authorisation nonce;
POST actions are not replayed from the read cache.

## Stable background refresh, October 8, 2026

Routine refreshes leave plots at full contrast and keep zoom, scrolling and
selections usable. The small Updating label appears after 500 ms; it never
delays a completed result. Overview/Search PNGs and visibility/calibration
rasters decode off-screen before their pixels, labels and timestamps commit
together. Failed image loads retain the previous complete view with an error.
An upper-triangle refresh retains the last complete matrix until all selected
pairs are ready; initial loading still shows bounded batch progress. The two
workers, 16-pair batches and four-window cache remain unchanged.

The SNAP live view keeps the previous labelled spectra across an acquisition
revision while the hourly replacement loads. Health and beamforming membership
use the separate bounded freshness rule above. No plotting estimator, backend
polling/acquisition cadence, hardware command, upload gate or service changed.
Existing Candidates and Source history use their stable item keys and saved
image caches; the history players keep their existing numeric buffers.

`scripts/check_refresh_stability_browser.py` uses intercepted browser-only
fixtures for delays, out-of-order reference responses, failed PNGs and health
expiry. `scripts/check_navigation_freshness.py` exercises saved live products,
failed GETs and both history players without operational writes. Neither test
establishes current telescope health or scientific correctness of a fixture.

October 6 navigation measurements with local Chromium and real data:

| Item | Before | After |
| --- | --- | --- |
| Visibility autos response | 7,662,297 bytes gzip | 167,747 bytes gzip |
| SNAP entry spectra | Two approximately 1.8 MB responses | One 632,763-byte gzip response |
| Search revisit, plot DOM ready | 0.65 s | 0.081 s |
| SNAP revisit, plot DOM ready | 1.73 s | 0.248 s |
| Overview revisit, plot DOM ready | 1.05 s | 0.298 s |

Visibilities revisits remained fast (0.076 s in this run), now with no repeat
snapshot download. Its first request after the web restart took 1.73 s; the
earlier 0.30 s measurement used a warm server cache and is not a cold comparison.
Scientific values, all channels, image resolution, masks and estimators are
unchanged. The main reduction comes from lazily requesting dynamic images only
for the selected quantity instead of eagerly sending every view with spectra.

An additional synthetic 80 ms / 10 Mbit/s link gave cached Visibilities/SNAP
returns of 0.074/0.244 s. This is browser network emulation, not a measurement
of the operator's SSH connection. Test route guards disable HTTP caching, so
Overview/Search PNGs were downloaded again in these tests despite their immutable
policy. DOM readiness does not measure physical display painting. See
`scripts/check_navigation_performance.py` and
`/home/casm/scratch/casm-observation-preview/navigation-after-20261006.json`,
`navigation-ssh-simulated-20261006.json`, and `navigation-freshness-20261006.json`.
Only the 8061 web service restarted; observing, weights, acquisition cadences
and worker/service limits were unchanged.

Validation: 180 backend regression cases, all 11 frontend test files, production
frontend build, real-data navigation/control checks, both history-player smoke
checks and injected refresh failures passed. Central guide, HTML/link/export
checks and wiki lint passed. The unrelated central scientific API snapshot was
not refreshed: its configured calibrator `calibration.py`, `results.py` and
`products.py` no longer exist upstream, and other recorded sources have drifted.
Its existing snapshot was preserved rather than partially regenerated.

The workspace on **8061** runs separately from the collector and three job
workers. The legacy 8060 and 8050 web services were stopped and disabled on
September 25, 2026; their data and backend services remain in place.
Opening Overview, Search, Visibilities
or Source history does not start acquisition, a calibration solve or a weights
build. It does read caches and render plots. Preview-local plot/cache writes
are allowed; the operational input stores remain read-only to these pages.

## Status latency fix, October 6, 2026

Latest-status reads previously grouped the entire scalar history for every poll.
Several page requests shared a database lock, so these scans could also delay
unrelated requests. The replacement seeks distinct names and their newest rows
through the existing `(name, ts)` index. It returns the same 288 current values
in the measured snapshot, including old keys; no retention, freshness policy,
schema, production data or acquisition cadence changed. Equal timestamps now
explicitly use the newest rowid, matching the single-scalar lookup.

Local sequential GET measurements, three samples per endpoint:

| Request | Before | After |
| --- | --- | --- |
| Overview | 2.09–2.31 s | 0.056–0.064 s warm; 1.18 s first request after restart |
| Visibility/array catalogue | 1.23–2.05 s | 0.039–0.051 s |
| SNAP beamforming membership | 1.20–1.23 s | 0.010–0.013 s |

Only the 8061 web service restarted. These are local shared-host measurements,
not complete browser load times or remote-network benchmarks. Scientific plot
rendering and large spectrum transfers retain their existing costs. Fingerprinted
frontend assets, including the unchanged 4.9 MB photo, now carry a one-year
immutable browser-cache policy. HTML and unversioned files require revalidation;
API responses do not inherit that policy. New builds use new asset filenames.

Evidence: `/home/casm/scratch/casm-observation-preview/`
`status-performance-20261006-before.json` and `status-performance-20261006-after.json`.
Reproduce with `PYTHONPATH=. python scripts/measure_status_reads.py --output <private-path>`
in the offline environment. It uses read-only SQLite and local GETs only.

## Measured September 24, 2026

The [capture report](screenshots/2026-09-24/measurements.json) records UTC times,
live service limits, CPU counter deltas, process RSS, cgroup anonymous/file
memory and screenshots. This is a shared-host, warm-cache observation, not an
isolated benchmark or a cold-load worst case. Browser RAM is not included.
The host has 96 logical CPUs; **100% CPU here means one core**, not the host.

During the 20-second quiet sample, the preview used about **0.6 GiB resident
RAM** and **0.2% of one core**. The subsequent loaded-page captures had sampled
RSS below 0.7 GiB. Overview/Search renders consumed CPU briefly; already cached
visibility and source plots were cheaper. First-time native source reads are
slower: the live four-hour date requests during acceptance took approximately
15 seconds each, with dates loaded sequentially. Do not extrapolate the
warm-cache capture to those first reads or to a calibration job.

The preview's cgroup total was about **3 GiB**, of which about **2.4 GiB was
filesystem cache**. Linux can reclaim file cache; this is not 3 GiB of Python
objects. Its configured `MemoryMax` is **4 GiB**, including that cache and child
processes. The web services have no CPU quota; request bounds, serialized large
reads and cache reuse limit routine work, not an absolute CPU ceiling.

Other services are separate budgets. The capture includes their counters:

| Service | Memory limit | CPU limit | Work |
| --- | ---: | ---: | --- |
| Preview web, 8061 | 4 GiB | None | Page APIs, plots, memory caches |
| Legacy web, 8060 (retired September 25) | 4 GiB at measurement | None | Historical measurement only |
| Collector | 8 GiB | None | Ingestion, retained shards, summaries |
| Production jobs | 256 GiB | 16 cores | Existing offline job queue |
| Preview calibration worker | 64 GiB | 4 cores | Explicit offline calibration jobs |

The jobs/calibration cgroups had tens of GiB of charged file cache even when
their main processes had small RSS. A main-process RSS number also excludes
active child jobs: use the whole cgroup for its budget. No worker limits were
changed for this audit. The supplied telescope JPEG is 4.9 MB, cached by the
browser; its decode and plot images add browser memory beyond the server totals.

## SNAP playback sample, September 29, 2026

Saved-history playback uses existing shards, not fresh SNAP reads. The original
full-board JSON path took about 0.6 seconds and 1.80 MB for one frame, too slow
for 4× playback's 250 ms target. Selected-input packed batches measured
**1.16–1.47 seconds and 3.21 MB for 12 frames of 16 inputs**, versus roughly
21.6 MB for twelve full-board JSON responses. One batch supplies three seconds
at 4×; the browser prefetches the next. Every native channel remains present.

Fixed-axis manifests took about 0.75 seconds for a day and 2 seconds for 48 hours.
A first week request took 7.7–11.1 seconds, scanning the retained reads once;
repeat requests reuse small extrema summaries. The two runs' sampled web RSS
stayed below 0.5 GiB. These were local shared-host GETs after web restarts, with
warm filesystem caches. The second run overlapped browser acceptance requests;
CPU and memory therefore include that traffic. They are not isolated load tests,
browser-memory measurements or guarantees for many simultaneous viewers.

Evidence is under `/home/casm/scratch/casm-observation-preview/`:
`snaps-playback-measurements.json` and
`snaps-playback-buffered-measurements.json`. Reproduce with
the offline environment and `PYTHONPATH=. python scripts/measure_snap_playback.py --output <private-path>`.
This does not contact hardware, clear filesystem caches or change service limits.
The browser retains two batches of up to 12 frames, plus the displayed frame
while seeking. No movie files or server-side frame cache are created.

At the September 29 measurement, Visibilities and Search playback were not
implemented. Visibility playback is now documented in
[visibility playback](visibility-playback.md); Search playback remains absent.

## Where products live

Paths below are the current corr1 configuration, not portable defaults for
every installation. `CASM_MONITOR_OBSERVATION_ROOT` selects the preview root.

| Path | Contents and writer |
| --- | --- |
| `/home/casm/scratch/casm-observation-preview/science/<id>/` | Local detailed/Overview renders: `plot-N.png`, `data.npz`, `metadata.json` |
| `.../t1_products/<hash>.png` | Immutable full and compact Search figures |
| `.../snap_views/<id>/` | Local renders of saved SNAP spectra, including `spectrum.png` |
| `.../hella_gulps.sqlite` | Preview-local bounded Hella log/gulp ledger |
| `.../investigations.sqlite` | Local review records; saved evidence remains referenced/copied locally |
| `.../calibration/` | Separate calibration-worker queue, logs and generated artifacts |
| `.../screenshots/` | Acceptance-test screenshots and reports, not telescope data |
| `/mnt/nvme3/casm_monitor/monitor.sqlite` | Collector-owned state, shard manifest and candidate summaries |
| `/mnt/nvme3/casm_monitor/shards/` | Collector-owned visibility, Kafka and SNAP shards; full-band `snap_read/` spectra have no expiry |
| `/mnt/nvme3/casm_monitor/jobs/`, `cal_builds/` | Production offline-worker artifacts |
| `/mnt/nvme4/data/casm/visibilities_64ant/` | Original native visibility files, not webpage output |
| `/mnt/nvme4/data/casm/hella_cands/` | Existing search outputs read by the collector |
| `/mnt/nvme5/casm_pipeline/db/t2.sqlite` | Existing T2 injection/candidate ledger, read-only here |
| `/mnt/nvme5/casm_pipeline/candidates/`, `weights/registry/` | Existing candidate/weights records, read-only here |
| `/mnt/nvme3/T3/EVENTS/` | Existing injection replay and event artifacts, served in place |
| B0329 directories named in `casm-wiki/detections.md` | Existing PDMP PNGs, served in place; no new filterbanks or folds |

`.../` means `/home/casm/scratch/casm-observation-preview/` in this table.
The whole preview tree measured about **1.5 GiB**: roughly 0.9 GiB scientific
plots/data, 0.46 GiB calibration artifacts, 51 MiB Search PNGs, plus local
SQLite ledgers and other files. It is on the **root filesystem**, not NVMe5.
The report contains exact per-directory bytes and mount/device mappings.

Array-overview payloads and the four source-transit galleries stay in bounded
process-memory caches, not new visibility or plot files. Source responses
contain PNG tiles and JSON numerical values. The source LRU holds 12 dates;
the array overview holds four snapshots and the pair cache has a 64 MB cap.
Neither page copies the raw visibility archive.

## Retention and refresh

The collector expires unpinned/unreferenced visibility shards after **3 days
native** and **60 days avg8**; Kafka full/subband TTLs are 14/90 days. Those
TTLs apply to manifest-owned shards, not raw files or preview render products.
The preview gulp ledger has its own bounded retention in `hella_log.py`.

**No automatic expiry currently covers preview `science/`, `t1_products/`
or `snap_views/` render artifacts.** A changed rolling interval can create a
new immutable product, so these directories can grow while pages are used.
Review records may refer to them. A retention policy must preserve reviewed
evidence before any cleanup is enabled. Nothing was deleted in this task.

Overview live cards refresh every 30 seconds and history every two minutes;
the array inspector refreshes each minute and Search every five minutes while
visible. Historical selections pause their rolling plots. Source history
loads available dates on selection/search; it is not a continuously refreshing
recording pipeline. Switching quantities/zoom on an already loaded array
snapshot is a browser display operation, not a new native read.

## Reproduce the evidence

With the preview already running, from the repository root:

```bash
/home/casm/software/dev/casm_venvs/casm_offline_env/bin/python \
  scripts/capture_monitor_docs.py --output docs/screenshots/YYYY-MM-DD
```

The script takes real loaded-page screenshots, samples memory every second,
and measures CPU by counter differences. It permits only local plot-render
POSTs and refuses acquisition/review/job writes. It does not restart services.
Do not use fixture-driven test screenshots as evidence of the live array.
