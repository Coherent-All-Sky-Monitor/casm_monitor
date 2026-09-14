# casm_monitor: independent CASM monitoring web service

## Current workspace supersedes the historical plan

Operator requirements were refined on 2026-09-13: dark scientific presentation,
no Plotly, selection-driven time/frequency and baseline exploration, rolling
24-hour injection review, per-gulp T1 evidence, calibration-day comparisons,
independent stationary Cyg A checks, B0329 history and requested investigations.
Observation, Readiness and Antennas remain the primary navigation groups.

Implemented contracts: [api-science.md](api-science.md),
[api-review.md](api-review.md) and [api-commissioning.md](api-commissioning.md).
The SNAP workspace adapts existing `/api/snaps/history` through
`POST /api/snap-workspace/render`, with bounded reads and saved scientific
figures. The initial static overview in `api-observation.md` remains supporting
evidence, not the current exploration workflow.

The isolated port-8061 workspace reads production data without changing it and
stores figures, queue evidence and manually confirmed canonical Sun builds under
its explicit artifact root. Newly seen misses persist until human review.
Requesting investigation records intent; no runner or Slack integration starts.
Deployment, restart defaults, SNAP operations, injections and dumps remain
disabled. Grafana is deferred; Fourier Space/Kafka code is outside edit authority.
Canonical scope: casm-wiki/monitor-product-direction.md and
casm-wiki/monitor-operator-workflow.md.

## Historical plan, 2026-09-08

The text below preserves the original design and decisions. Its light styling,
Plotly stack, deployment controls, active-count examples and historical milestone
claims do not describe or authorize the current workspace. Do not implement an
old requirement below in preference to the current contracts above.

Plan file. Repo: `/home/casm/software/dev/casm_monitor` (empty, remote
`Coherent-All-Sky-Monitor/casm_monitor`, branch `main`). Written 2026-09-08 after
surveying casm-wiki, casm_io, casm_vis_analysis, casm_calibrator,
bf_weights_generator, casm-bf-imaging, casm_beam_scheduler, casm_t2/t3, the
Fourier-Space stat pipeline (Kafka/Redis), and the SNAP control stack on zapdos.

## Context

The team has three partial UIs and no single place to see the array: the
Streamlit webconsole (:8501, ad-hoc vis inspection, 6.6 GB RSS, no history),
t3-web (:8050, candidates only), and a PHP "Stats" page on corr1 port 80 fed by
Kafka -> Redis (per-input thumbnails, no history, broker hand-started in tmux with
data in /tmp). Array health facts (dead inputs, EQ steps, ant 26 excess, voided cal
after a re-sync, wrong window for a Sun solve) are currently discovered days late
by re-reading visibility files by hand.

Goal: one long-running, self-contained service on corr1 (`casm-monitor.service`,
systemd --user, bound to 127.0.0.1, reached by `ssh -L`) that (1) collects live
data continuously into its own history store, (2) serves tabs for SNAPs,
Visibilities, Imaging, Calibration and, later, Candidates (T2/T3), and (3) runs
the standard calibration playbook on demand with the canonical driver, gated so
that the actual weights upload stays a human action per the standing wiki rule.

## What it looks like

Top strip on every page (auto-refresh 10 s), the "is the array alive" line:
obs state + UTC_START (LMC :20300), deployed weights product id and cal file
(registry `/mnt/nvme5/casm_pipeline/weights/registry/` cross-checked with
`deployed_weights.csv` last row), hella SNR/DM_MIN per node, SUB_INCOH flag,
Kafka/Redis/t2d/zapdos liveness with "last seen" ages, SNAP PPS status, disk free
on nvme3/4/5, GPU utilisation, Sun/Cyg A/Cas A altitude now and next transit.
Everything stale turns amber; anything > 2 cadences old turns red, never blank.

Tabs:

1. **SNAPs** — one card per board (.52 .51 .62 .73, plus relays .59 .68 .69
   shown as PPS-only). Two layers per card, clearly labelled with their age:
   (a) the correlator-side bandpass from Kafka (10 s, 3072 ch, no hardware
   contact) is the live layer; (b) the board-side read via zapdos (4096 ch,
   375-500 MHz with the correlator's 390.6-484.4 MHz shaded, ADC RMS vs the
   5-30 LSB healthy band, EQ epoch, feng_id from the packetizer BRAM) is taken
   **once every two hours** by the collector (operator 2026-09-08 evening: "once an hour or two is plenty"; config `snap.read_interval_s: 7200`) and otherwise **only when a user clicks
   "Read boards now"**; the card shows the last read with its timestamp until
   then. Board reads are serialized (one ssh session, boards in sequence,
   a lock so two clicks cannot overlap, minimum 5 min between manual reads)
   so zapdos and the packet path are never polled continuously. A **history
   toggle** switches the card from live to a time slider over the store:
   scrub any past spectrum (10 s Kafka frames for 90 d at subband
   resolution, 60 s full-res for 14 d, hourly board reads forever), or view
   a waterfall of the input's bandpass over the chosen span, the night-median
   trend (dB) with EQ/gain-change epochs marked, and the diagnosis legend
   (flat = dead feed, pinned = railed ADC, all-zero subband = F-engine
   delivery). Antenna labels from `AntennaMapping.load()` so the card says
   `ant 26 | N16E1 | S2A1 | input 25`.
2. **Visibilities** — cached sub-matrix = every WIRED input (`functional=1`
   in `antenna_layouts/current`, re-read at each integration so the set grows
   by itself as antennas are connected; 24 today = 300 baselines). Default
   DISPLAY = the live/beamforming set (`include_in_beamforming=1`, 17 today).
   Selector: live set | all wired (cached). DECISION (operator, 2026-09-08):
   only these two sets for now, to keep data rates manageable; an
   "all 48 SNAP inputs" view (from raw files on demand) is deferred and can
   be added later. Measured 2026-09-08 (casm_io, live file): read time per
   integration 0.07 s (24 inputs), 0.15 s (48), 0.31 s (full 8256-baseline
   triangle), so compute is never the limit at the 137 s cadence; storage is:
   4.6 GB/d for 24 inputs, 18 GB/d for 48, 32 GB/d at 64 antennas full-res.
   Retention: full-res 3 d, 8x channel-averaged 60 d (0.6 GB/d today, 4 GB/d
   at 64 antennas). Views: autocorr grid by SNAP, cross amplitude and phase
   matrices, per-baseline phase-vs-freq (sawtooth) and waterfalls, night
   coherence matrix, and the missing-subband panel. Every visibility view has
   two toggle bars (operator, 2026-09-08): **quantity** = amplitude | phase |
   real | imag | coherence (crosses only, |V|/sqrt(A_i A_j)), and **units** =
   linear | dB | log10 for amplitude/real/imag (real/imag in dB use sign x
   10log10|x|, labelled as such), degrees | radians for phase, with an
   optional reference: raw, fringe-stopped toward the Sun, or divided by the
   deployed cal. The choice is a URL query so a view can be bookmarked and
   shared. Time slider over the history store; "live" pins to the newest
   integration.
3. **Imaging** — all-sky dirty image per integration (l/m zenithal projection,
   `allsky_snapshots`) with the deployed cal, source markers, PSF ceiling,
   24 h movie strip; per-source cutouts (`image_around_source`) around whichever
   of Sun/Cyg A/Cas A/Tau A is up. History: one thumbnail per integration.
4. **Calibration** — solve source is the **Sun** (the only calibrator the
   array is sensitive enough to solve on today, operator 2026-09-08); Cyg A,
   Cas A, Tau A, Vir A and RA/Dec appear greyed as "not yet", enabled by one
   config flag when the array gets there. Pick
   window (default: the source's altitude maximum on the chosen day, computed
   with `source_altaz`, offset shown explicitly), static window (default: the
   quiet window of the night before, from `plot_quiet_window_altitudes` caps),
   antenna set (default: layout `include_in_beamforming`, with the wiki
   exclusions preloaded), ref ant, beam layout: standard 512 exact grid, or
   N beams toward a source plus fill from the deployed grid (built with
   casm_beam_scheduler's validated snapshot builder, `fill.mode from_weights`,
   because the driver's `grid_mode="track"` is only a bounding box, not
   beams along a track). "Run" launches
   `bf_weights_generator.make_cal_and_weights.run(RecipeParams(...))` as a
   background job with a live log. Results page shows the driver's own figures
   (phase sawtooth raw/fringe-stopped/calibrated, gain delay fits,
   `svd_vs_freq` sigma_1..6, rank-1 vs freq labelled "solve quality, not beam
   quality", `beam_check` vis-domain Cyg A/Cas A response, cal_diff vs the
   deployed cal, beam grid, transit coverage), the report.json numbers, the
   six-check verification status, and the exact deploy command. Deploy stage:
   the app runs the dry-run (`deploy_bf_weights.py` without `--upload`) into a
   stage dir and shows md5s + SCALE pairing read from the current
   `deployed_weights.csv` row. Upload: a real button, enabled only after that
   dry-run, uploads on a human click with the safeguards in Decisions.
   A "compare cals" view overlays two products' referee coherence.
4b. **Search** (operator, 2026-09-08) — live distributions of the raw hella
   (T1) candidates: histograms and 2-D densities over SNR, width, DM, beam,
   time (SNR vs DM, DM vs time, beam occupancy map on the 512-beam grid,
   width-SNR encoding per `crab-gp-search.md`), per-node/per-job rates, the
   10k-cap saturation indicator (beams searched per gulp), plus the T2 funnel
   from `gulp_stats` (cands -> clusters -> stored -> triggers). Source: tail
   `/mnt/nvme4/data/casm/hella_cands/cands_<UTC_START>.dat.{0..7}` (7 cols
   `snr samp time_days width dm_idx dm beam`, appended per 8.19 s gulp,
   tsamp 1.048576 ms, beam global 0-511, jobs 0-3 corr1 / 4-7 corr2 pulled
   over ssh or rsync) and the t2 sqlite read-only. NEVER the t2d sockets
   (12345-52 belong to t2d). History: per-gulp binned counts kept forever,
   raw rows for 7 d. Toggle bars for linear/log axes and time window.
5. **Candidates** — port casm_t3's web routes as a prefix-aware FastAPI
   router under `/cands` (its templates use absolute redirects and it writes
   label/FRB rows, so a plain mount is not safe); labeling is kept, on the
   same sqlite. t3-web stays up until the port is verified, then retired.
6. **History/Events** — a timeline of detected state changes: input died/
   recovered, ADC railed, EQ/gain changed, subband went dark, obs restarted,
   weights uploaded, cal job run. This is the page that replaces re-excavating
   incidents.md for "when did ant X change".

## MVP direction for plots (operator, 2026-09-09 evening)

Heavy figures are rendered SERVER-SIDE with the team's own matplotlib code
(casm_vis_analysis plotting), for the last 24 h, refreshed every 30 min, and
served as PNGs with ETag + max-age so the browser caches them and toggles are
instant. Interactive Plotly stays only for click-to-expand single-baseline views.
Reason: hundreds of tiny Plotly widgets look worse than one matplotlib raster and
load one by one. Real-time is not required; keeping up with data is.

## Architecture

```
zapdos: snap read (casm_f, read-only) --ssh, hourly or on click--> |
corr1 Kafka casm_antenna_{bp,ts,hg} (10 s)  --consumer-->      | collectors
corr1 /mnt/nvme4/.../visibilities_64ant (137 s)  --memmap-->   |  (one process,
LMC :20300, redis, registry, ps, df, nvidia-smi  --poll-->     |   asyncio)
                                                               v
                       history store: /mnt/nvme3/casm_monitor/
                         sqlite (scalars, events, jobs)  +  zarr (spectra, reduced vis, images)
                                                               ^
FastAPI app (:8060, 127.0.0.1) + Plotly.js (vendored) ---------|
job runner (subprocess per cal/imaging job, casm_offline_env) -|
```

- Package `casm_monitor/` with `collectors/`, `store/`, `web/`, `jobs/`,
  `deploy/systemd/`. Two units: `casm-monitor-collect.service`,
  `casm-monitor-web.service` (Restart=always, linger already on).
- Env: `casm_venvs/casm_offline_env` (has fastapi/uvicorn/jinja2, casm_io,
  casm_vis_analysis, casm_calibrator, bf_weights_generator, casm_imaging).
  Add: `zarr`, `kafka-python` (or `confluent-kafka`), `redis`, `plotly` (for the
  vendored JS). pip through the proxy.
- Three units: `casm-monitor-collect`, `casm-monitor-web`, and
  `casm-monitor-jobs` (single worker, durable SQLite job queue with leases,
  restart reconciliation marks orphaned jobs failed, cancel + timeout,
  `MemoryMax=256G`, `CPUQuota` capped so live pipelines keep their cores).
- Web reads only the store; collectors write only the store. Arrays are
  written as immutable time shards (write to temp name, fsync, rename) and
  published by one SQLite transaction in a manifest table; readers only see
  committed shards, and retention deletes only unreferenced shards. No shared
  zarr appends between processes.
- Kafka: CLIENT ONLY, zero broker footprint. The broker, its config, topics,
  retention and producers belong to Fourier Space and are never touched.
  The consumer uses group-less `assign()` on the three topics and never
  commits offsets, so nothing is written into the broker; the read watermark
  lives in our SQLite. Frames are assembled from the six per-subband
  producers with a completeness timeout, keyed (producer, stream, timestamp)
  so replays are idempotent. Schema is captured from live messages as the
  first M1 task and pinned in a fixture. The app never depends on Kafka being
  alive: if it dies the SNAP cards show the last board read and the bus as down.
- Store on /mnt/nvme3 (5 TB free). Never nvme5 (97% full).
- Server-side decimation: every spectrum/waterfall endpoint takes a viewport
  and returns at most ~1e6 cells from pre-built min/max/mean pyramids
  (60 s, 10 min, 1 h), so the browser never receives a raw 14-day array.

### Feasibility numbers

| stream | raw rate | what we keep | store growth |
|---|---|---|---|
| Kafka bandpass, 6 producers (one per subband, corr1 0-2 + corr2 3-5), 10 s | ~590 KB per assembled full-band frame | full-res every 60 s for 14 d; 96-subband every 10 s for 90 d; night median per input forever | ~0.85 GB/d + 0.16 GB/d |
| zapdos SNAP spectra, 4x12x4096, hourly + on demand | 0.8 MB | every read, forever | ~20 MB/d |
| vis, 8256 bl x 3072 ch x 8 B per 137 s | 203 MB/integration, 127 GB/d | wired inputs only (300 bl today): full-res 3 d + 8x chan-avg 60 d | 7.4 MB/integration; ~14 GB + 36 GB today, ~100 GB + 240 GB at 64 ant |
| all-sky image 241x241 per integration | - | float16 image + PNG | 0.2 GB/d |

Reading 300 baselines out of a memmapped integration is sub-second. The live
file grows in place (verified: `.dat.82` mid-write at 4.67 GB), so the vis
collector uses the exact header offset and the exact integration size
(3072 x 8256 x 8 B = 202,899,456 B), keeps a one-integration guard band behind
the file end, requires size and mtime stable across two polls, persists an
(obs, file, integration) watermark in SQLite, and never crosses a file
boundary in one read (the casm_io part-boundary memmap bug).
Conclusion: the Visibilities tab is feasible on the current node with no new
hardware; the only thing that would not be feasible is caching all 8256
baselines, which nobody needs.

Imaging cost MEASURED 2026-09-09: allsky_snapshots (241 px, 17 ant, deployed cal, freq_avg 32, 8 workers) = 5 snapshots in 26 s, peak RSS 142 MB, i.e. ~5 s per integration; imaging every integration costs ~1 CPU-hour/day. Imaging cost was the one unknown: `allsky_snapshots` is CPU-parallel
(`workers`), `freq_avg=32`; must be benchmarked (target < 60 s per 137 s
integration on 16 cores; fall back to every 2nd/4th integration). GPUs 4/8/9
are idle if needed later, but do not plan on them.

## Recommendations beyond the ask

1. **Referee trend (optional, off by default).** Fringe-stop Cyg A/Cas A
   with the DEPLOYED cal and record coherent-beam coherence vs an off-beam
   null (the wiki's `rank1-metric-caveat.md` method) as a daily sensitivity
   trend. Diagnostic only; nothing in the cal path depends on it, since the
   array calibrates on the Sun alone today.
2. **Sample-slip watchdog.** After any obs restart or SNAP event, run the
   weights-verification check (f) (per-antenna delay diff vs the last cal;
   4.00 ns multiples = slip) and raise a red banner "cal voided, re-solve". This
   was the 2026-08-19 root cause and is cheap to automate.
3. **EQ/gain provenance.** Snapshot EQ coeffs and ADC gains from every board
   daily and on demand; tag every spectrum with the epoch, so dB comparisons
   across the 2026-09-02-style changes are refused, not silently plotted.
4. **Step detector on night medians** (1-2.5 dB steps = hardware intervention,
   the ant 18/26 histories) writing to the Events tab; optional Slack post
   through the same proxy t3-collect uses.
5. **Transit planner panel** from the exact array-factor tables
   (`casm-bf-source-transit`), never the ellipse.
6. **Position check job** (`casm-fit-positions` on a transit, against several
   references) offered next to the cal job; the ant 18 0.8 m error would have
   shown up here.
7. **Do not use Streamlit** for this: it re-executes the page per interaction,
   holds tens of GB, and has no place for a collector. FastAPI + a vendored
   Plotly.js keeps the browser doing the interaction and the server doing I/O.

## Decisions (operator, 2026-09-08)

- **Upload: dry-run then a real Upload button; a human click uploads.** The
  click is the human action the wiki rule requires, but the rule's wording
  ("the --upload step itself is a human action, not something to automate
  unattended") gets a `decisions/2026-09-xx-monitor-upload-button.md` record
  and a one-line edit in `weights-and-deploy.md` at M3. Safeguards in code:
  the button is disabled until the dry-run for that exact product succeeded;
  a confirm dialog requires typing the product tag; the service refuses if
  `casm-track` is running, if the HDF5 `format_type` is wrong, or if the SCALE
  pairing differs from the ledger row without an explicit override; every
  click writes an audit row (who/when/md5s/command) and calls the registry
  function directly (the deploy script's own registry call is known not to
  fire until `b0329-fixed-cells` merges); no scheduled or API path can reach
  the upload handler, only the browser form with a CSRF token; the service
  runs with `--upload` capability only when `CASM_MONITOR_ALLOW_UPLOAD=1` is
  set in the unit's environment.
- **Stack: best current tooling, not a match to past tools.** Backend
  FastAPI (async, WebSocket/SSE push, BackgroundTasks) in `casm_offline_env`;
  frontend a React + TypeScript app (Vite) using Plotly.js for spectra,
  matrices and images and uPlot for dense time series, built once and
  committed as static assets so the server never needs node at runtime
  (node 18 is on corr1, npm goes through the 10.70.0.1:8118 proxy; if the
  registry is unreachable, fall back to vendored plotly.min.js + htmx).
  Store: SQLite (WAL) for scalars/events/jobs and zarr for arrays. Considered
  and rejected: Streamlit (re-runs the script per click, tens of GB, no
  collector home); HoloViz Panel/Bokeh (Python-only and good at streaming,
  but a per-session server, weaker for embedding the T3 pages and for a
  multi-tab app); Grafana + TSDB (excellent for scalar alerts, useless for
  3072-channel spectra and images; can be added later on top of the SQLite
  scalars if wanted).
- **SNAP source: both** Kafka bandpass (10 s, live) and zapdos board reads
  (every 2 h + on-click only, never continuous; operator instruction 2026-09-08).
- **Order:** M0 scaffold, M1 SNAPs, M2 Vis, M3 Cal, M4 Imaging, M5 Cands.
- **Roles:** Fable orchestrates; Opus 5 (xhigh) implements the collectors,
  store and cal job runner; Sonnet 5 (medium) does templates, tests, docs;
  Luna does mechanical greps; Sol reviews each milestone read-only.

## Milestones (each ends with a running service, a wiki update, a local commit)

- M0 scaffold: package, config, store schema, systemd units, status strip, the
  Events table, tests. Port 8060.
- M1 SNAPs: Kafka consumer + hourly/on-click zapdos read + history slider + tab.
- M2 Visibilities: reduced-vis collector + tab.
- M2b Search: hella cands tailer (both nodes) + gulp_stats + tab.
- M3 Calibration: job worker around `RecipeParams`/`run` with the layout
  snapshotted into the job dir (date, sha256, active set shown; CB/IB
  populated slot counts must equal the active set or the product is
  rejected), results page, staged dry-run, Upload button with safeguards,
  wiki decision record; source-directed beams via casm_beam_scheduler.
- M4 Imaging: benchmark wall-clock and peak RSS first (a 47-integration cal
  solve alone materialises ~9.5 GB through casm_io; imaging per integration
  unknown), then all-sky per integration + source cutouts under the job
  worker's admission control.
- M5 Candidates: port t3 router; slip watchdog; referee trend (optional).

Reuse, never rewrite: `casm_io.correlator.VisibilityReader` / `read_visibilities`
(`casm_io/casm_io/correlator/reader.py`), `AntennaMapping`
(`casm_io/casm_io/correlator/mapping.py`), `casm_vis_analysis.fringe_stop`,
`.sources`, `.plotting.phase_freq`, `casm_calibrator.svd_calibrate/SVDConfig`,
`bf_weights_generator.make_cal_and_weights.{RecipeParams,run}`,
`recipe_diagnostics`, `recipe_verify`, `exact_grid.generate_beam_grid_exact`,
`deploy_bf_weights.py`, `casm_imaging.imaging.{allsky_snapshots,
image_around_source, psf_for_result, BaselineSet}`, casm_t3 web templates,
`casm_f.snap_fengine.SnapFengine` + `plot_snap_autocorr.read_autocorr_spectra`
on zapdos.

**Additive only, never interfere with the medusa/Fourier-Space flow
(operator instruction 2026-09-08):** the service never modifies fourier-space
code, configs, the PHP Stats page, `/data/casm/stats/...`, or Redis keys; it
reads Redis read-only and consumes Kafka under its own consumer group (Kafka
is pub/sub, other consumers are unaffected); it binds only its own port; it
never restarts or signals any medusa daemon; its CPU quota and NUMA pinning
stay off the cores medusa pins (`numactl` lists in the casm_ant_stat lines).

Hard rules baked into code: no code path may call `program_*`, `health_sweep`,
`set_coeffs`, `--do_sync`; one serialized reader per board; input = packet_idx
= antenna-1; `FrequencyConfig.layout_64ant()` and `freq_order="descending"`
explicit; exact grid only; SCALE read from the ledger, never hardcoded; kill
`casm-track` before any obs restart; exactly 512 beams.

## Verification

- Unit tests per collector with recorded fixtures (one Kafka message, one
  integration slice, one SNAP npz).
- Store round-trip and retention tests.
- `systemctl --user status casm-monitor-*`; `ssh -L 8060:127.0.0.1:8060 corr1`
  and walk each tab; compare the SNAPs tab to `snap_ops/plot_snap_autocorrs.py`
  output and the vis tab to `casm-autocorr` for the same integration.
- Cal tab: rebuild the 2026-08-31 CAL0830N product through the app and md5 the
  HDF5 datasets against `/mnt/nvme5/vishnu/cal_build_20260831/`
  (the `tests/validate_recipe.py` pattern).
- Imaging: Cyg A cutout SNR against the wiki's recipe (d)4 numbers.
- Load: collector RSS < 4 GB, web RSS < 1 GB, no GPU use, no writes outside
  `/mnt/nvme3/casm_monitor/`.

## Independent review (Sol, 2026-09-08)

Sol's read-only review of the first draft returned no-go with ten objections.
Incorporated above: exact-byte live-file reads with guard band and watermark
(1), Kafka schema/consumer-group contract and corrected 0.16 GB/d (2),
immutable shards + SQLite manifest instead of shared zarr appends (3), a third
job-worker service with a durable queue (4), memory benchmarking and
MemoryMax before M3/M4 (5), source-directed beams via casm_beam_scheduler
because `grid_mode="track"` is a bounding box (6), layout provenance and slot
count check per job (7), viewport decimation (9), t3 as a prefix-aware router
(10). Not taken: deleting the Upload button (8); the operator chose the button
and the safeguards in Decisions apply. Review text:
`/home/casm/.claude/jobs/52a9cda8/tmp/sol_review.txt` (job tmp, copy into the
repo's `docs/reviews/` at M0).
