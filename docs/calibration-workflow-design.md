# Calibration workspace proposal

Date: 2026-09-22. Requested by Vishnu for the port-8061 workspace.
Status: approved design; first implementation described in
`calibration-workspace.md`. The detailed controls below remain the target design;
the implementation manual identifies supported controls and explicit limitations.
September 25 authority supersedes the proposed deployment controls: the workspace
is now dry-run-only, with no upload button and server-side upload denial. The
ellipse sky view is display-only; scientific generation remains exact. See the
implementation manual for current behavior; the deployment sections below are
the earlier design, not permission to enable them.

## Navigation and persistent context

Add Calibration alongside Observation, Readiness and Antennas. Inside it use
Prepare, Solve & inspect, Validate on sky, Generate beams, and Deploy & verify.
Keep a compact build-history selector and current recorded product above these
views. Show candidate versus recorded deployment explicitly, with evidence time.
Do not infer live GPU state from a ledger entry or a successful file copy.

Every action belongs to one immutable build revision. Save source/static windows,
actual integrations, layout snapshot, antenna set, reference, channel/time masks,
recipe/code versions, calibration identity, diagnostics and review decisions.
Changing scientific inputs creates a revision and invalidates dependent results.
Changing a plot zoom or colour scale does not rerun the solve. Plot controls stay
distinct from solve masks. Source data remain read-only; outputs are retained.

## 1. Prepare

| Control | Recommended initial behaviour |
| --- | --- |
| Calibrator | Sun enabled; Cyg A and Cas A visible as future solve choices. Validation against them is available independently. |
| Date and solve window | Latest complete Sun transit with available data; 60 minutes centred on calculated altitude maximum, editable. OVRO local clock with UTC switch. |
| Data coverage | Show actual integrations, missing spans, frequency/subband availability and source altitude before enabling Calibrate. Cross-midnight selection must work. |
| Antennas | Start from recorded deployed membership, show current layout inclusion alongside it, and highlight differences. Editable station-labelled selection and reference antenna. |
| Layout | Resolve and freeze `antenna_layouts/current` for a new product; use dated mappings for historical visibility data. Do not rewrite the global layout. |
| Static subtraction | Matched recent night template, off, or comparison with/without static. Show Sun/Cyg A/Cas A altitudes and hardware-epoch compatibility for its window. |
| Comparison calibration | Recorded deployed reference by default; allow another saved candidate. Mixed-calibration products need their actual beam-to-calibration assignments. |

Advanced controls: duration/offset from transit, minimum altitude, explicit RFI
channel and integration exclusions, baseline groups for plots, and additional
candidate antenna sets. Masks must be passed through supported canonical APIs,
not implemented as an independent web solver. The first version should expose
only controls the worker can record and reproduce.

Keep phase-only per-channel SVD, fringe-stop sign, conjugation convention,
frequency ordering, masked-channel policy and int8 quantisation as fixed recipe
settings. They are inspectable metadata, not casual switches. Static subtraction
must not silently change across an EQ, wiring or synchronisation boundary.
Do not reject an old calibration solely because of age; show known hardware
changes separately. Unknown hardware compatibility is not a verified match.

## 2. Calibrate and inspect

One Calibrate action queues the canonical driver with `make_weights=False`,
diagnostics and an executed notebook. Show stage progress and completed artifacts
as they appear. A completed process with skipped diagnostics remains incomplete
for scientific review. No typing a UUID merely to run an offline solve.

Organise plots in readable sections instead of one unstructured image gallery:

- Input spectra and flags: per-antenna autos, integration coverage, channel mask,
  and all six subbands.
- Phase progression on the same baselines and frequency axes: raw wrapped
  sawtooth, fringe-stopped, instrumental-delay-fit residual, full-calibration
  residual. The last two are different diagnostics and need different labels.
- SVD: singular-value spectra, explicitly named rank-1 ratio/fraction, gain
  phases, per-antenna delay fits and residual RMS, solved-channel fraction.
- Comparison: new versus recorded calibration phase/delay differences and
  possible whole-sample slips; static/no-static comparison when requested.
- Cross-day sawtooth: solve day plus up to three recent available days, matched
  solar-transit offset/duration, common baselines and frequency selection,
  candidate and reference calibration overlays. Label each day's static choice.

Useful display controls: baseline by length/orientation/stations, selected/all
reference baselines, wrapped/unwrapped phase, full/selected band, day selector,
candidate/reference overlay, show masked channels, shared axes, download PNG,
CSV/NPZ, report and executed notebook. Defaults show a few representative long,
medium and short baselines; antenna membership must never change through a plot
filter. Rank-1 is solve structure, not a deployment score.

Operator clarification, 2026-09-22: provide BOTH phase-versus-frequency curves
and frequency-versus-time phase waterfalls, matching the visibility tab and
the walkthrough notebook. A Curve / Waterfall / Both selector changes only
presentation. Raw, Fringe-stopped and Delay-subtracted stages are available in
both representations, with an optional full-calibration residual stage. Offer
side-by-side comparison with a common baseline, time/frequency selection and
fixed cyclic phase colour scale (-pi to pi). Source selection for fringe stopping
is independent of which sources are enabled for SVD solves: Sun, Cyg A and Cas A
should be available for these diagnostic views through the maintained source API.

The selected source's contribution should lose its time-dependent geometric
fringes after fringe stopping; subtracting the fitted instrumental delay should
flatten its frequency-dependent phase ramp. Remaining phase offsets, bandpass
structure, other sources and noise remain visible, not forced to zero. Record
the delay-fit interval/band and keep the fit fixed when applying it to other
times. Use the notebook's maintained `fit_delay` / `apply_delay` APIs. Curves may
show one selected integration or the phase of a complex average over a selected
interval, never an arithmetic average of wrapped phase angles.

## 3. Validate on independent sky data

Run transit check selects a saved candidate calibration, Cyg A by default,
another source/date, and an available visibility interval. Start with about
five hours around transit, exposing actual off-source coverage. A source used
for solving is a self-check, not independent validation.

The primary result reproduces the stationary-beam reference: fix the steering
at the source's transit alt/az; use cross-correlations only; subtract an off-source
complex median per baseline/channel; render P(time, frequency) above its light
curve. Do not substitute the existing short tracking-beam diagnostic.
The operator's explicit visual reference is
`/mnt/nvme5/casm_pipeline/scratchpad/2026-09-08_cyg_a_stationary_beam_core17_ant18_E1.png`:
frequency on the upper panel's y-axis, hours from meridian transit on the shared
x-axis, static-subtracted coherent power as colour, and a peak-normalised light
curve below. Include the transit marker, measured peak marker, peak offset,
FWHM and peak/off-RMS annotation. Keep signed power and masked-channel gaps.
Adapt its styling to the dark app while preserving this plot structure. Its
historical antenna-18 position and calibration are example provenance, not
defaults for the new workflow. Absolute light curves and reference overlays are
additional review options; they do not replace this requested two-panel figure.
Show measured peak offset, FWHM, peak/off-source RMS and frequency-dependent
timing alongside the exact array-factor prediction. Missing off-source coverage
or failed half-max crossings makes the relevant metric unavailable.

Compare candidate and reference on the same integrations and a common channel
mask. Offer common antennas for isolating calibration changes, and actual
product antenna sets for evaluating the operational change. State which is in
use. Plot absolute power as well as optional peak-normalised shape; normalised
curves alone hide sensitivity changes. Do not call peak/off-RMS absolute SEFD.

Controls: source/date/window, candidate/reference, common band, fixed pointing
computed from transit or explicitly chosen, off-source intervals, off-beam
control, absolute/normalised light curves, frequency slices, exact-model overlay.
Choose controls by angular separation and predicted response; a large azimuth
offset near zenith may not be a null. Optional expert comparisons: tracking
beam, self-cal ceiling, static variants, selected add-one/leave-one-out trials.
These are labelled separate experiments with an explicit compute cost.

## 4. Generate the 512-beam product

Reuse the reviewed calibration through the canonical driver's `cal_path`.
Generate exactly 512 alt/az beams with `grid_mode="exact"` and the selected
active geometry. Use exact array-factor coverage, not analytic ellipse spacing.
Expose minimum elevation and preview coverage, zenith coverage, source tracks
and beam IDs; keep grid objective under Advanced. Initial values may be copied
from a recorded recipe, but must not be guessed from an old product's extrema.

Generate and inspect the matching IB companion. Verify selected antennas against
actual populated CB/IB slots, excluded slots are zero, frequency/channel order,
six-subband occupancy, int8 range, format/shape, and stated-pointing coherence.
The private inclusion snapshot must match the reviewed selection to avoid the
driver's silent intersection with old layout flags.
Show changes to beam coordinates, membership, calibrations and any fixed custom
cells. Replacing custom/factorial cells with a plain grid requires an explicit
choice. Incompatible per-beam antenna sets and a single IB mask need a defined
amplitude contract before they can become an uploadable package.

## 5. Deploy and verify

Provide Prepare upload, Upload reviewed product, and Restore previous product.
Prepare upload runs the existing dry run, retains hashes and per-node payload
inspection, and shows candidate-versus-current differences. The final human click
confirms the product tag, optional note, activation time if supported, and whether
to save it as the restart default. Reuse the existing server-side authorization,
hash/layout/scale checks and deployment lock. Do not require repeated approval
for every local plot. Changing a staged payload requires a new dry run.

Read the CB/IB SCALE pairing from verified configuration and report conflicts;
never hardcode a historical pairing or expose it as an unqualified slider.
Check tracking/scheduler conflicts at execution, not only page load. Persist
per-subband progress and partial failures. Do not blindly repeat the entire push
or claim atomic deployment across nodes. Restore is another reviewed upload.

Report these outcomes separately:

1. CB and IB FIFO transfers completed for the six beamformer subbands.
2. The exact product and activation events are present in the weights registry.
3. If requested, restart-default payload hashes match on each node's served
   subbands: corr1 0–2, corr2 3–5. A timestamp/header change is not payload drift.
4. Runtime activation is confirmed only by an available consumer acknowledgement
   or equivalent identity evidence; otherwise show activation unverified.
5. Data flow and candidate/injection evidence continue after the change; this
   operational check is distinct from proof of coherent sensitivity.

## Implementation map and acceptance

Existing foundations: `web/commissioning.py`, `web/cal.py`, `jobs/cal_build.py`,
`jobs/deploy.py`, `web/science_transit.py`, and the unused older calibration pages.
The 8061 `/cal` route currently uses CommissioningPage, is CB-only, and has no
enabled deployment path. Do not revive the old pages without reconciling their
contracts with isolated workspace outputs and the canonical worker.

The current transit adapter only accepts the ledger calibration, native cache,
two hours and 55 integrations. Extend the maintained science implementation to
support the reviewed five-hour stationary waterfall, candidate catalogue and
common-mask comparisons, then call it from a durable background job. Reuse
`casm-bf-sawtooth` for historical comparisons. Do not copy scratchpad algorithms
into HTTP handlers or keep scientific jobs supervised by a web-server thread.

Use persisted job stages with cancellation, output manifests, resource estimates,
one bounded worker, and explicit handling of interruption. Rerun only dependent
stages after edits. Confirm all required checks are present before upload review;
do not invent universal numerical acceptance thresholds from one historical run.

Build order: workspace and persistent revisions; canonical solve plus diagnostic
gallery; independent transit and cross-day jobs; exact grid and paired CB/IB
verification; deployment integration and receipts. Acceptance includes a saved
known-good dataset matching the reference notebook numerically, an antenna-change
comparison on a common mask, interrupted jobs, missing diagnostics, staged-file
drift and partial-upload handling. Actual telescope upload remains a later human
action on a specific reviewed product.

## Evidence inspected

- `/home/casm/software/dev/casm-wiki/recipes.md`
- `/home/casm/software/dev/casm-wiki/weights-and-deploy.md`
- `/home/casm/software/dev/casm-wiki/weights-verification.md`
- `/home/casm/software/dev/casm-wiki/weights-build-2026-09-17.md`
- `/home/casm/software/dev/casm-wiki/rank1-metric-caveat.md`
- `/home/casm/software/dev/casm-wiki/decisions/2026-09-09-monitor-upload-button.md`
- `/home/casm/software/dev/bf_weights_generator/bf_weights_generator/make_cal_and_weights.py`
- `/home/casm/software/dev/bf_weights_generator/bf_weights_generator/deploy_bf_weights.py`
- `/home/casm/scratch/cal_build_20260917/stationary_beam_transit.py`
- `/home/casm/software/dev/casm_vis_analysis/notebooks/casm_calibration_and_beamforming.ipynb`
- `/mnt/nvme5/casm_pipeline/scratchpad/2026-09-08_cyg_a_stationary_beam_core17_ant18_E1.png`
- `/mnt/nvme3/vishnu/cal_build_20260917/cal_sep17_core16_exact512_diagnostics.ipynb`
- `/mnt/nvme3/vishnu/cal_build_20260917/weights_sep17_core16_how_built.ipynb`
