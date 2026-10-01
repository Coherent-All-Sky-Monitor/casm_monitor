# Calibration workspace

Implemented September 22 and restyled September 25, 2026 at `/cal` on 8061.
The five steps use larger controls, green antenna selections, white phase/transit
figures, a compact plot gallery and the shared click-to-zoom viewer. Existing
results come before collapsed settings; jobs and downloadable files remain saved.
Historical PNGs retain their original pixels; newly rendered diagnostics use
the white style. The old Commissioning page is superseded.
October 1 controls use gray for untouched steps, amber for the active/running
step, green for reviewed preparation or saved outputs, and red for a failed or
cancelled job. Opening a step does not complete it. An imported reference does
not count as a new solve/grid; green sky validation means a saved independent
transit to review, not scientific acceptance. Upload turns green only after
local payload checks pass and remains explicitly **not uploaded**.

![512-beam sky view, September 25 screenshot](screenshots/2026-09-25-calibration/sky.png)

[Phase views](screenshots/2026-09-25-calibration/phase.png) ·
[Stationary-transit views](screenshots/2026-09-25-calibration/transit.png)

## Operator flow

1. **Prepare**: choose the solar date and up-to-four-hour solve/static windows,
   antenna membership, reference and hardware-epoch note. The initial date is
   the day before the newest raw-observation filename date, not a coverage
   guarantee. Current layout bytes and the privately adjusted inclusion flags
   are saved separately. A new scientific selection requires a new tag.
   Solve and static time entry default to **OVRO local (America/Los_Angeles)**;
   the UTC toggle changes display only, preserving the selected instants.
   The Sun altitude maximum shows its altitude and both dated local/UTC times.
   The solar date is the Sun-maximum date, the same civil day in UTC and at OVRO;
   the matched static window can fall on the previous local evening. Recipes
   and API timestamps remain UTC. Invalid or repeated DST-hour edits block
   submission; choose UTC explicitly for either occurrence of a repeated hour.
   Antennas default to the recorded deployed CB weights, not the layout flags.
   On October 1 the selection matched the SNAPs deployment view's 16 antennas;
   antennas 12 and 33 were layout-only and unselected. This was a saved-product
   comparison, not a fresh GPU readback. Missing deployed weights retain the
   existing labelled layout fallback.
   **Choose a static window** plots the Sun, Cyg A, Cas A and Tau A from
   the previous local noon through the selected day's end (36 hours normally,
   35/37 across DST). It follows the same local/UTC toggle, shows the current
   solve/static intervals and supports zoom/download. The canonical
   `casm_vis_analysis.offsource.find_quiet_windows` supplies intervals of at
   least 15 minutes using `DEFAULT_ALTITUDE_CAPS_OVRO`: Sun/Tau A below 0°,
   Cyg A below 20°, Cas A below 15°. Cas A never fully sets at OVRO.
   A **Use 30 min** button (or a shorter qualifying duration) fills a centred
   interval and clears human review; it does not start a job or silently replace
   defaults. If no interval qualifies, the page says so. Altitude warnings are
   advisory: RFI, missing data and hardware compatibility still need checking.
   Source tracks and cuts are sampled each minute; these are geometric
   altitudes, not a terrain horizon or a visibility-data quality measurement.
2. **Calibrate**: the canonical `make_cal_and_weights` driver runs with
   `make_weights=False`, phase-only per-channel SVD, diagnostics and notebook.
   No grid, upload or live layout edit occurs. A failed job retains its log.
3. **Solve & inspect**: choose up to three reference baselines and Sun, Cyg A
   or Cas A as the diagnostic fringe-stop direction, over up to four hours.
   Phase views default to the full solve window. Produce raw,
   fringe-stopped and fitted-delay-subtracted curves AND frequency/time phase
   waterfalls. If the revision has a static window, it is also shown and used.
   The curve is the phase of the complex mean; the fixed delay fit uses the
   selected above-horizon interval. Presentation changes do not rerun jobs.
   Canonical cross-day sawtooth comparisons use up to three matched solar
   windows with the solve's duration (up to four hours), three longest common
   baselines, and no recycled static. API callers may set `minutes` from 1 to
   240 for a shorter or longer history view. API and worker revalidation share
   the same window limit; grid generation reuses the saved calibration.
4. **Validate on sky**: a five-hour stationary beam, fixed at the source's
   transit alt/az. Cross-only visibilities have a per-baseline/channel complex
   off-source median removed. Candidate and recorded reference share antennas,
   integrations and the intersection of their good-channel masks. Turning off
   comparison uses the candidate's own antenna set. Raw signed power,
   peak-normalized shape and exact ideal geometry are retained separately.
   Channel peak times use a three-integration boxcar, a user-selectable
   peak/off-RMS threshold, and edge-peak rejection. Chromaticity is measured
   relative to the exact geometric model; it is not an automatic pass/fail.
5. **Generate beams**: the first plot restores the legacy ellipse sky layout
   and Sun/Cyg A/Cas A/Tau A altitude tracks. It reads actual saved pointings;
   glyph widths are the approximate 450-MHz FWHM, for display only. No ellipse
   hit times or coverage scores are computed. Before generation it explicitly
   shows the last uploaded grid as a reference. Changing the UTC track date
   only renders/caches a local PNG; it never generates or changes weights.
   Canonical `cal_path` reuse creates exactly 512 exact-grid
   beams and the matching IB mask using the configured hash-pinned generator.
   Minimum elevation is editable; custom/factorial cells are explicitly not
   preserved. Only newly solved workspace candidates can generate a grid.
6. **Upload dry run**: require a produced notebook with executed, error-free
   code cells, independent-source transit, CB/IB product and human review note.
   Stage through the existing deploy dry run, with `--no-registry`, no `--upload`
   and no `--save-defaults`. The workspace forces upload authority off and its
   middleware rejects upload requests even if parent configuration is armed.
   There is no live-upload button. Stage checks verify 512 payload beams and
   matching metadata/pointing counts, as well as CB/IB formats and populated slots.

**Inspect recorded calibration** imports the ledger reference without a solve.
It can produce new phase/transit diagnostics, but cannot become an uploadable
product by bypassing solve provenance. Ledger identity does not prove live GPU
identity, particularly for mixed-calibration products.

## Persistence and service

`CASM_MONITOR_OBSERVATION_ROOT/calibration` contains its own `monitor.sqlite`,
`jobs/<id>/log.txt` and `cal_builds/<tag>/`. Each action has an immutable UUID
directory, recipe/result JSON and downloadable PNG, NPZ, notebook or HDF5.
The submitted recipe and frozen layouts are hash-checked before execution.

`deploy/systemd/casm-monitor-calibration.service` runs the existing durable
worker, independently of web restarts. One process works at a time; every
analysis action has a one-hour execution deadline (not a data-window limit)
and a 64 GiB address-space cap. The
service also has a 64 GiB aggregate memory limit, CPU quota and reduced priority.
Cancellation uses the existing worker process-group termination. Interrupted
jobs fail by lease reconciliation; they are never silently reported complete.
This workspace is offline-only. Do not arm its worker.

## API and checks

- `GET /api/calibration[?date=YYYY-MM-DD]`: defaults and saved candidates.
- `GET /api/calibration/sky-plan?date=YYYY-MM-DD`: cached source altitudes and
  sky-only static-window suggestions. No visibility reads, template generation,
  jobs or hardware contact; 32 dates retained in process, 16 in the browser.
- `POST /api/calibration/builds`: reviewed solve request.
- `POST /api/calibration/import-reference`: diagnostic-only ledger reference.
- `GET /api/calibration/builds/{tag}`: recipe, jobs, results and artifact catalog.
- `POST /api/calibration/builds/{tag}/actions`: phase, transit, history or grid.
- `POST /api/calibration/builds/{tag}/review`: explicit scientific acceptance.
- `GET /api/calibration/jobs/{id}/log`, `POST .../{id}/cancel`: durable job control.
- `/api/calibration/deployment`: the existing guarded calibration deployment
  router with isolated settings, review gate and live uploads forced off.
- `GET /api/calibration/builds/{tag}/sky?date=YYYY-MM-DD`: cached schematic
  sky view; PNGs are in `calibration/sky_views/`, outside weight build products.

All workspace POSTs require the loopback same-origin request and
`X-CASM-Workspace: 1`. Generic job submission cannot invoke this privileged kind.
Artifact URLs are catalogued, contained and reject escaping symlinks.

Acceptance: `tests/test_calibration_workspace.py`, existing `tests/test_cal.py`,
`casm_vis_analysis/tests/test_calibration_checks.py`, frontend production build,
`scripts/check_calibration_browser.py` and `scripts/check_calibration_science.py`.
`scripts/check_calibration_times_browser.py` checks the served membership,
local-first solve/static controls, date rollover, UTC round trips, DST errors
and responsive layout with all non-GET API requests blocked. It never submits
a solve, import, diagnostic or upload. Pure conversion cases are in
`frontend/tests/time-zones.test.mjs`.
Sky-planning acceptance includes September 18 (no qualifying interval), May 16
(two intervals, applying one changes only the form), all four canonical source
tracks, DST day lengths, zoom/download and gray/amber/green workflow states.
Tests: `tests/test_calibration_planning.py` and
`frontend/tests/calibration-progress.test.mjs`.
`check_calibration_science.py` explicitly submits bounded real-data analysis; its `--solve` option
also generates a new offline canonical calibration. It never uploads.

Documentation checks: the central Sphinx build, local-link and export checks
pass. The source snapshot drift check reports changes across all three science
repositories; a complete refresh remains blocked by its configured
`casm_calibrator/calibration.py` source not being a tracked file. This task does
not replace that historical snapshot with a partial refresh.

Offline acceptance on September 22 produced saved Sun solve and executed
notebook, exact 512-beam CB plus matching IB, and real September 18 phase and
Cyg A diagnostics. The example revision is
`workspace_acceptance_20260918_1790102134` in the isolated calibration store.
These are interface/science-path acceptance products, not a scientific
deployment approval; no live weights, registry or restart defaults were changed.
`scripts/check_calibration_reference.py` reproduces the saved September 18
core16 stationary-beam spectrum with matched pointing/background selections:
relative RMS difference `1.10e-7` over 131 integrations × 3072 channels.
Evidence: `calibration/reference-validation.json` under the preview root. This
validates the cross-only calculation in that domain, not the calibration quality.

## Explicit limitations

Source/static hardware-epoch compatibility still needs human review; availability
is established by the actual reader, not inferred from filename presence.
Historical layout association is dated-file inference for imported references.
Transit geometry is ideal equal-amplitude, without an element-beam/sky model.
Common-mask comparison is implemented; separate simultaneous operational-set
comparison, automatic RFI selection, add-one trials and off-beam controls are
not part of this first version. There is no automatic restore button or remote
consumer-identity acknowledgement. Transfer, registry, restart-default and runtime
receipts remain distinct; unmeasured outcomes are explicitly unverified.
