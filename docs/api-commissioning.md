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

`GET /api/sources?q=B0329` returns `sources[0].attempts` with every B0329 row from
the canonical `casm-wiki/detections.md` ledger, newest first. Rows preserve date,
configuration and complete outcome text, including non-detections, nulls and
retractions. Multiple qualified S/N/width measurements in one row are retained
as prose; `snr` and `width_ms` are null rather than guessed from the first number.

Saved PNGs are discovered only under explicit ledger directories and that date's
wiki evidence folder: at most two nested levels, 3000 directory entries and
80 images per attempt. Raw/filterbank/dump subtrees are excluded. Each artifact
has an opaque identifier and URL; symlinks and out-of-root paths are refused.
The September 13 read-only check found 22 attempt rows and 250 associated images.
This is archive discovery, not new folding or scientific reclassification.

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
