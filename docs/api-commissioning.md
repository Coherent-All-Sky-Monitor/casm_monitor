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

Sun/Cyg A visibility-transit history is not implemented in this change. The
choice between a selected-baseline dynamic spectrum and a source-directed
visibility beam remains open. Neither is a PDMP/filterbank fold.

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

## Documentation impact and checks

This adds user-triggered local compute, scientific-product review and saved
source-history access. Central `guides/monitoring.md` must describe their controls,
limits and disabled deployment, and link the existing generation, calibration
and folding tutorials. No central scientific example needs rerunning.
`tests/test_commissioning.py` exercises isolated staging, preserved wiring,
review/hash gates, canonical-only execution through a stub, artifact containment
and retention of non-detection/retraction history. No production store is written.
