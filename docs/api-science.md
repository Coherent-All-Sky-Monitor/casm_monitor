# Scientific exploration API

The observation preview renders selected cached visibility data with the existing
CASM scientific Matplotlib routines. Native recordings are read only in the
explicit bounded `recorded` mode. No Plotly, telescope commands, weights
generation or deployment occur through these endpoints.

## Select and render

The opening page automatically renders rolling-24-hour cached raw phase and
amplitude views on one geometry-selected long N-S baseline, alongside T1 plots.
The baseline explorer automatically renders its cached selection on entry and
after changes. Rolling windows refresh every two minutes while visible (the T1 stream page
every five minutes); choosing a historical day/range pauses rolling. Native reads and calibration comparisons
still require an explicit Render action. No worker or raw fallback was enabled.
Cached request budgets and evidence limitations are unchanged.

Plot/selection clocks default to America/Los_Angeles (PDT/PST), with a UTC
toggle. Dates use midnight boundaries in that zone; API bounds remain UTC.
Spring-forward nonexistent times are rejected. A repeated fall-back hour selects
its earlier occurrence; use UTC for the other occurrence. Phase waterfalls
label clock time, not hours elapsed from the first sample.

`GET /api/science/catalog` returns input labels, ENU baseline lengths and
orientations, plank groups, three long NS defaults from the matching inspected
recorded payload union (falling back to intended participation),
canonical dated layouts and actual cache availability. Intended participation
does not assert antenna health. `preset_source` states the membership evidence.
NS/EW labels mean the
minor horizontal component is no more than 10% of the major component.

`POST /api/science/render` accepts:

```json
{"pairs": [[8,18]], "t0": 1789315200, "t1": 1789316200,
 "fmin": 410, "fmax": 470, "kind": "phase_waterfall",
 "reference": "sun", "resolution": "avg8",
 "time_tz": "America/Los_Angeles"}
```

Pairs are ascending packet indices, not antenna IDs. Kinds are
`phase_waterfall`, `amplitude_waterfall`, `phase_spectrum`,
`amplitude_spectrum`, and `autos` (diagonal pairs). References are `raw` and
`sun`. Native cached `full` resolution requires a window at most six hours;
`avg8` allows seven days. At most six selected pairs, 4,600 integration rows
and 12 million complex cells are admitted per interval. Missing full-resolution
data returns an error; it never falls back to raw files or a full-shard read.
One plot request runs at a time; concurrent requests receive 429.

`resolution=recorded` is an explicit native-recording read, not a fallback.
It allows at most one hour per interval and 64 MiB of selected complex samples.
The requested end must be at least two integrations behind now to avoid reading
an accumulating write. The existing `casm_io.read_visibilities` reads only the triangle among the
requested inputs, with frequency selection, one worker and the configured
visibility directory. It never recursively scans `/mnt` or reads the full
correlator triangle. Native first-of-file samples and wholly zero-filled missing
records are omitted. Reader failures, including known part-boundary problems,
return an error rather than substituting another dataset. Per-part file size
and modification time enter artifact identity. This mode supports calibration
days whose native cache has expired but whose local recording remains.

Results include `id`, `images`, `metadata_url`, `data_url`, exact `selection`,
`provenance`, and `warnings`. Products are immutable and cached by request,
source-shard identities and layout file identity. Download links use
`GET /api/science/products/{id}/{filename}`. PNG files, `metadata.json` and
`data.npz` are allowlisted; no arbitrary filesystem URL is exposed. NPZ arrays
are `vis` (T, baseline, F), `time_unix`, `freq_mhz` and packet-index `pairs`.
They contain the actual reference-transformed values plotted, not raw files.

## Scientific interpretation

Phase spectra use the angle of a complex time mean, never a mean of wrapped
phase angles. Solar waterfalls reuse `plot_dynamic_spectrum`, plotting
`|cached V|` normalized by each channel's own selected-window mean. Amplitude
spectra use a time mean of magnitude, in linear counts; autos are already power
and are not squared again. Missing values remain missing; known first-of-file
junk integrations are omitted and counted. Gaps in phase waterfalls are masked.

The singleton Sun-delay adapter preserves `(time, baseline)` axes explicitly.
An older `atleast_2d` conversion produced `(1, time)` for one baseline and
broadcast the time axis into spurious baselines. The preview fixes this in the
shared monitor transform; phase plotting now rejects inconsistent shapes.

The reduced collector has already complex-averaged eight frequency channels
before fringe-stopping. This can cancel rapidly rotating phase; downstream
fringe-stopping cannot reverse that loss. Full cached resolution is the detailed
phase diagnostic. Nominal integration timestamps are retained: midpoint/end
interpretation is not independently established for every observation.

The cache has packet IDs but no layout hash. Association with canonical dated
layouts is therefore inferred and disclosed. Selections crossing dated-layout
boundaries are rejected. Experimental layout variants cannot be selected. A
future collector provenance change is needed for proven layout association.

## Product-led calibration-day comparison

The comparison page starts with the ledger-recorded calibration/product reference,
not two unexplained time controls. `GET /api/science/calibration-references`
returns its adjacent canonical report, source solve window, reported antenna set
and product context. The report must identify the calibration through its
canonical `out_dir`/`tag` output path and contain a Sun `source_window` of at most
one hour. No date is inferred from the calibration filename. Missing or mismatched
evidence produces an unavailable state instead of a guessed calibration day.

`comparison_date=YYYY-MM-DD` selects an OVRO local date, default today. The
proposed interval matches the reference's local clock bounds and duration,
with daylight-saving offsets applied per date. Both intervals, report evidence,
calibration antennas and recorded product appear before rendering. A solar
window that has not completed is not shortened: choose an earlier day. The
comparison day is initially limited to the past week. Matching clock windows
does not guarantee identical Sun geometry or unchanged analog conditions.

One long N-S baseline is selected initially, restricted to antennas in the
reference's recorded recipe. Up to three baselines can be compared. Only the
explicit **Read both windows and compare phase** action requests native recordings,
at most one hour and 64 MiB of selected complex samples per window. It reuses
`/api/science/render`, `kind=phase_spectrum`, `reference=sun`, `resolution=recorded`,
with selected-day `t0`/`t1` and reference-day `compare_t0`/`compare_t1`.
`calibration_reference_id` binds those exact windows, native resolution,
Sun phase processing and baseline membership to the verified report. Stale IDs
or mismatched selections are rejected before data access; the reference and
report hash are included in saved provenance.

The same baseline and frequency axis are used, with separate Sun fringe-stopping
for each date. Different layout epochs are rejected. No deployed calibration is
applied, no solution is built, and changed phase does not constitute deployment
approval. A mixed factorial weights product may use several calibration solutions;
the ledger reference is not asserted to apply to every beam.

Waterfalls render as one landscape panel per baseline with horizontal station
and geometry titles. Full antenna/SNAP/ADC labels remain in provenance. Phase
uses a fixed −π..π color scale, and date labels are readable on dark surfaces.

## Requested stationary Cyg A check

`POST /api/science/transit` accepts `calibration_id` from catalog
`transit_calibrations`, optional `recorded_product_id` as context, `t0`, `t1`,
`fixed_alt_deg`, `fixed_az_deg`, `control_alt_deg`, `control_az_deg`, `fmin`
and `fmax`. It requires native cache, at most two hours within one UTC day,
55 integrations, 2–32 calibration antennas all wired in the dated layout,
and at most 500 MiB of selected native complex samples. No avg8 or raw fallback.

The existing `beam_power_vs_time` computes calibrated cross-only power for
two explicitly fixed directions. A compact selected triangle and correspondingly
remapped `AntennaMapping` preserve the original physical antenna IDs. Missing
antenna data removes the integration rather than silently changing membership.
Calibration flags and the selected frequency band are honored.

`array_factor_response` computes the exact ideal equal-amplitude geometry
response for both directions, using 32 frequencies. A local geometry-only HDF5
feeds that maintained API; it contains no deployable weights or calibration.
The ideal model is shown on a separate normalized axis, with no fitted offset
or amplitude that could force agreement. The model excludes element-beam
response, amplitude taper and the detailed flag pattern. Measured cross-only
power and ideal total normalized power are not directly amplitude-comparable.

No static background subtraction is performed. The short view may not contain
the full transit. This is a diagnostic and not a calibration pass/fail verdict.
The selected ledger calibration is identified independently of optional recorded
weights context: a mixed-calibration beam product is not one uniform solution.
Saved numerical evidence includes both measured curves and model responses.

## Documentation impact

This API changes monitoring interaction, not the scientific package APIs.
The central monitoring guide must describe range/geometry selection,
downloadable evidence, cache resolution and calibration comparison limits.
No calibration examples should be regenerated to document this router.
