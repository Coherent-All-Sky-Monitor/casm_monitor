# Commissioning and source history workspace

These routes belong to the isolated observation preview. They do not use the
production calibration queue. Set `CASM_MONITOR_OBSERVATION_ROOT` to a directory
outside the production monitor store. Mount `web.commissioning.build_router(settings)`
and `web.source_history.build_router()` in the application.

## Reviewed Sun build

`GET /api/commissioning?date=YYYY-MM-DD` returns the existing Sun recipe defaults,
the wiring list, local build records and authority limits. Dates are UTC.
The existing defaults carry ledger-derived calibration/product information;
they are a starting point for review, not a new verification of live payloads.
The proposed static window must be checked for sky and hardware changes.
`recorded_diagnostics` links existing reports, notebooks and figures adjacent
to the ledger products, through `/api/commissioning-recorded/artifacts/{id}`.
These are historical evidence, not freshly rendered diagnostics. A factorial
weights file can use several calibrations; its ledger calibration is one
reference, not proof that every beam uses that calibration.

`POST /api/commissioning/stage` takes `source`, `tag`, `source_window`,
`static_window`, `antennas`, `ref_ant` and `reviewed: true`. Windows use the existing
calibration API's UTC syntax. Each source/static window is limited to one hour.
The existing calibration admission validator checks Sun-only operation, time
bounds, reference antenna, recipe invariants and approved IB-generator identity.
This last check is inherited admission policy; this endpoint does not run that
generator.

Staging writes a UUID-scoped private layout snapshot, recipe JSON and state JSON.
Only functional antennas can be selected. The snapshot's inclusion flags match
the reviewed selection, avoiding the driver's silent intersection with old
inclusion flags. It never edits `antenna_layouts/current` or CASMAN. The response
contains `id`, `state: staged`, `recipe`, `command`, hashes, resource `budget`,
warnings and an initially empty `artifacts` list. A staged request is not a build.

`POST /api/commissioning/{id}/start`, body `{"confirm_id":"<id>"}`, is the explicit
human build action. It rechecks recipe/layout hashes, current wiring and free
space, acquires a process lease, then starts only:

```text
<casm_offline_env>/bin/python -m bf_weights_generator.make_cal_and_weights --config <isolated recipe.json>
```

The existing canonical driver supplies all calibration, exact-grid, validation,
plot and notebook implementation. No new solver, reader or calibration plotting
algorithm is added. The web wrapper enforces a 30-minute wall/CPU budget,
24 GiB per-process address-space limit and 2 GiB per-file limit, and requires 10 GiB free
before launch. CPU and address-space limits apply to individual processes, not
an aggregate cgroup; the wall deadline covers the process group. These are
limits, not runtime/memory predictions. One process
lease prevents concurrent builds across web processes. Logs, caches and products
stay in the isolated build directory. No expensive build was run to test this API.

The canonical driver generates a new exact 512-beam grid. This does not preserve
custom B0329 factorial cells from the recorded deployed product. It writes CB
weights and diagnostics; the required paired IB generation/inspection remains a
separate step. Deployment and restart-default replacement are disabled, with no
deploy-script or registry invocation. A successful process enters `review_required`,
not `approved` or `deployed`. Review notebook `SKIPPED` sections explicitly.

`GET /api/commissioning/{id}` returns state and allowlisted PNG/report/notebook
artifacts; `GET /api/commissioning/{id}/log` returns at most the final 128 kB.
`GET /api/commissioning/{id}/artifacts/{artifact_id}` serves only discovered files
inside that build. If the web process exits during a build, a running record may
remain unresolved: inspect its log and process state before taking any action.
The API labels a record whose web supervisor no longer exists `interrupted`,
with completion unknown; a surviving builder still holds its exclusion lease.
There is no automatic retry, restart or deployment.

The canonical source-tracking beam diagnostic is not the separately requested
stationary Cyg A transit experiment. Rank-1 and phase drift alone do not establish
beam sensitivity or authorize deployment. The historical stationary experiment
and its exact-response comparison remain separate scientific workflows.

## B0329 history

The page opens with detection dates and one large saved fold per date, newest
first. One caption identifies PDMP folds from dumped filterbanks; long outcome,
configuration and directory text is inside **Notes & saved plots**. Click a
plot to enlarge and zoom it. **All observations** includes non-detections,
disputed attempts and withdrawals. Grouping combines same-calendar-date rows
without dropping their notes. A date with no accessible image stays visible.
The September 24 check found 13 detection dates and 19 observation dates.

`GET /api/sources?q=B0329` returns `sources[0].attempts` with every B0329 row from
the canonical `casm-wiki/detections.md` ledger, newest first. Rows preserve date,
configuration and complete outcome text, including non-detections, nulls and
retractions. Multiple qualified S/N/width measurements in one row are retained
as prose; `snr` and `width_ms` are null rather than guessed from the first number.

Each row's `status` is one of `non_detection`, `contested`, `detection` or
`recorded_attempt`. `detection` is set only when the outcome prose (outside any
backticked filename) contains the word DETECTION and does not contain
NON-DETECTION, and the row is not `contested`; a plot merely named
`..._detection.png` does not set it. Status is never inferred from `snr` or any
number in the text. Eight older ledger rows describing successful folds without
that literal word have reviewed detection overrides, keyed by their exact
SHA-256-derived row IDs in `LEGACY_DETECTIONS`. Any change to one of those rows
invalidates its override; neither its date nor a high S/N alone carries it forward.

Saved PNGs are discovered only under explicit ledger directories and that date's
wiki evidence folder: at most two nested levels, 3000 directory entries and
80 images per attempt. Raw/filterbank/dump subtrees are excluded. Each artifact
has an opaque identifier and URL; symlinks and out-of-root paths are refused.
The September 13 read-only check found 22 attempt rows and 250 associated images.
This is archive discovery, not new folding or scientific reclassification.

`artifacts[0]` is the headline plot: every backticked `.png` basename named in
the row's outcome, then its directory text (brace patterns like `name.{png,log}`
expanded, full paths matched by basename), is promoted to the front of
`artifacts` in that order. A name not found among the row's own scanned
artifacts is borrowed from another row sharing the same `YYYY-MM-DD` date
prefix (same URL, not a copy). `headline_from_ledger` is true when
`artifacts[0]` was named this way, false when it is merely the first PNG the
scan happened to find. `HEADLINE_PDMP` adds reviewed per-row PDMP preferences,
including the June 3 SVD fold instead of its null StEFCal control and the
August 24 high-altitude fold instead of its extended low-altitude trial.
Those preferences only reorder discovered files, never create artifact paths.
`headline_selection: reviewed_pdmp` records that selection; `headline_from_ledger`
still states whether its basename was explicitly named in the row. Filenames
and all associated plots remain in the expandable notes; associated controls
and unsuccessful folds are not relabelled as detections.
The preferred path suffix also disambiguates identical filenames in different
fold directories. `headline_artifact` is null when the reviewed detection plot
is unavailable, even if other associated files exist. In particular, the May 25
date remains visible but its free-DM artifact-fit PNGs stay in Notes rather than
representing the recorded at-par result. August 4/5 select checked DM-locked
folds, not the old broad-artifact/uncorrected-period variants. Discovery also
admits the exact preferred PNG basename when it lacks `pdmp` or `fold`.

`artifact_scan_partial` reports a discovery cap. Missing files do not remove the
attempt. A directory-associated plot is not automatically a valid detection;
read the corresponding ledger qualifications. Unknown source queries return
`state: no_match`; a missing ledger is `unavailable`.

## Sun and Cyg A visibility history

Search **Sun**, **Cyg A**, **cyg-a** or **Cygnus A**, or use the source shortcuts.
Each date opens a source-tracking beam dynamic spectrum, with a transit marker,
white frequency/time axes and click-to-zoom. Dates and times on the page use
OVRO local time. These are visibility-derived beams, separate from B0329 PDMP
folds and from the stationary-beam calibration test.

`GET /api/sources?q=sun` (or `cyg-a`) returns `kind: visibility_beam`, the current
ledger calibration identity, native-cache coverage and available `transits`.
Catalog dates are UTC, newest first, at most seven dates, and require at least
three cached native integrations within ±1 hour of the source's meridian
transit. Normally only three days survive native-cache retention. The catalog
does not list future transit centers or treat a date as a detection.

`GET /api/sources/transits/{source}/{day}?calibration_id=<id>` accepts canonical
`source` values `sun` and `cyg_a`, a `YYYY-MM-DD` UTC date and the catalog's
32-hex calibration ID. A changed/unavailable calibration returns 409: refresh
history rather than silently using a different solution. No arbitrary product
paths, old averaged-cache fallback or raw archive scan are accepted.

The default is the latest deployment-ledger calibration, applied to **every**
displayed date, not a claim that it was deployed on that historical date or
that every beam in a mixed-calibration weights product uses it. The page names
the file; Plot details records the policy, dated layout, antenna IDs, shard IDs
and read budget. All 2–32 distinct calibration antennas must be wired in that
dated layout, without a silent subset or substitution. The current calibration
can therefore use a different antenna set from the Visibilities inspection preset.

The maintained `casm_vis_analysis.beam_power.beam_power_vs_time` API tracks the
named source with geometric sign −1 and applies `c_i conj(c_j)` once, at the
3072 native frequency channels. Calibration flags and nonfinite weights mask
channels. Stored-pair orientation and compact selected-triangle mapping retain
physical antenna identity. Output is signed cross-only power,
`2 Re(sum_{i<j} w_i conj(w_j) V_ij)`, **not** total tied-array power or `|V|`.
Autos are excluded; no static/off-source subtraction or flux calibration is
performed. Negative values remain valid and the units are calibration-weighted
correlator counts, not Jy. A bright tracking beam alone does not establish a
source detection or calibration quality.

The native read is limited to 55 integrations and 500 MiB of selected complex
samples; calibration metadata is limited to 32 MiB. Calibration and beamforming
precede frequency-only averaging to 128 display bins. Missing cross baselines
propagate to missing beam samples; flagged/missing channels remain in place
until display averaging, which averages available values within each bin.
First-of-file position never excludes a sample. Actual time gaps stay blank.
Each plot uses its own labelled symmetric linear colour limits; no channel
normalization is applied. Edge-truncated windows are labelled partial.

The response includes `tile` (PNG and colour scale), native `freq_mhz`, actual
`t0`/`t1`, `integration_s`, `samples`, `antenna_ids`, calibration and `details`.
`preview` contains `time_unix` (T), averaged `freq_mhz` (F≤128) and signed
`cross_power` (T×F), with nonfinite values represented by null. Its in-memory
LRU holds at most 12 results, keyed by source/date, calibration, layout and
shard identities; large reads share the array-overview lock. GET requests do
not write visibility, calibration or plot files, or start acquisition.

`tests/test_source_transits.py` covers aliases, dates, channel order, calibration
amplitude/phase, reversed packet pairs, signed values, excluded autos, missing
samples, native-only reads, cache invalidation and JSON output. Browser checks
are in `scripts/check_source_transits_browser.py`; the bounded live independent
Hermitian-matrix check is `scripts/check_source_transits_science.py`.

Preview dependency: the local `casm_vis_analysis` working tree already supplies
`beam_power_vs_time(return_spectrum=True)` and
`calibration_checks.transit_center`. Those existing calibration-workspace
additions are not committed by this monitor change. Preserve and review them
together before deploying from clean checkouts; the historical central API
snapshot does not describe these additions.

## Documentation impact and checks

This adds user-triggered local compute, scientific-product review and saved
source-history access. Central `guides/monitoring.md` must describe their controls,
limits and disabled deployment, and link the existing generation, calibration
and folding tutorials. No central scientific example needs rerunning.
`tests/test_commissioning.py` exercises isolated staging, preserved wiring,
review/hash gates, canonical-only execution through a stub, artifact containment
and retention of non-detection/retraction history. No production store is written.
