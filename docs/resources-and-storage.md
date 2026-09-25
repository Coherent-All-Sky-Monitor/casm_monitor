# Webpage resources and data products

The preview on **8061** is a separate process from production web **8060**,
the collector and the two job workers. Opening Overview, Search, Visibilities
or Source history does not start acquisition, a calibration solve or a weights
build. It does read caches and render plots. Preview-local plot/cache writes
are allowed; the operational input stores remain read-only to these pages.

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
| Production web, 8060 | 4 GiB | None | Production API/UI |
| Collector | 8 GiB | None | Ingestion, retained shards, summaries |
| Production jobs | 256 GiB | 16 cores | Existing offline job queue |
| Preview calibration worker | 64 GiB | 4 cores | Explicit offline calibration jobs |

The jobs/calibration cgroups had tens of GiB of charged file cache even when
their main processes had small RSS. A main-process RSS number also excludes
active child jobs: use the whole cgroup for its budget. No worker limits were
changed for this audit. The supplied telescope JPEG is 4.9 MB, cached by the
browser; its decode and plot images add browser memory beyond the server totals.

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
