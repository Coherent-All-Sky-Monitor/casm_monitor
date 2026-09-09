# Visibilities tab API contract (M2)

Written by the frontend agent for the backend agent implementing these routes
concurrently, per `docs/plan.md` section "2. Visibilities". The frontend
(`frontend/src/lib/api.ts`, `frontend/src/lib/types.ts`) is built exactly
against this; please implement to the letter or, if a field needs to change,
edit this file and ping so both sides stay in sync.

All timestamps in JSON bodies are unix seconds (float ok) unless noted.

**Backend notes (M2 implementation, 2026-09-08).** The routes are implemented in
`casm_monitor/web/vis.py` and the transforms in `casm_monitor/vis_ops.py`. Four
points where the served responses differ from the first draft of this contract;
the doc is now the served behaviour:

1. Times are **unix seconds**, with an ISO companion where one is useful:
   `obs.latest_ts` + `obs.latest_ts_iso`, `obs.oldest_ts` + `obs.oldest_ts_iso`,
   `spectra.ts` + `ts_iso`, `waterfall.t` + `t_iso`. `obs.utc_start` is the
   correlator's own UTC_START base string (`"2026-09-04-16:43:47"`), not an ISO
   timestamp: it is the observation's identifier, used verbatim in file names,
   the status strip and t2/t3.
2. `nchan` **defaults to 768**, not full resolution, and is clamped to
   `1..3072`; a request is further reduced so that
   `n_baselines * nchan <= max_cells` (default 400 000). The served `nchan` is
   echoed in the response, and can be one or two channels below what was asked
   for (the block size has to divide 3072 somehow).
3. An **inapplicable `quantity`/`units`/`ref`/`pairs`/`set` combination is a
   400** with a `detail` string, never silently ignored — including
   `quantity=coh&pairs=auto` (coherence is not defined on an autocorrelation).
   Valid unit sets: `amp|real|imag` -> `linear|db|log10`, `phase` -> `deg|rad`,
   `coh` -> `linear` only. `amp` in dB is `20 log10 |V|`; `real`/`imag` in dB are
   `sign(x) * 10 log10 |x|`.
4. Every response echoes `quantity`, `units`, `units_label`, `ref` and a
   `ref_meta` block (for `ref=cal`: `cal_file`, `cal_path`, `cal_source`,
   `cal_ref_ant_id`, `inputs_without_cal`; for `ref=sun`: `sign`,
   `sun_alt_deg`, `sun_below_horizon`), plus `transform_s`. `flags` carries the
   integration's own `first_of_file` (the first integration of every file is
   junk per the casm_io memory note: it is flagged, never dropped) together with
   `sun_below_horizon` / `cal_file` when those references were used.

## `GET /api/vis/inputs`

The cached sub-matrix inventory: which packet indices are cached in each of
the two sets (`live` = beamforming set, `wired` = all functional inputs), plus
the per-input facts needed for panel titles.

```json
{
  "sets": {
    "live": [0, 1, 2, 5, 9],
    "wired": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
  },
  "inputs": [
    { "packet_idx": 0, "antenna": 1, "station": "N21E1", "in_bf": true }
  ],
  "obs": {
    "utc_start": "2026-09-04-16:43:47",
    "latest_ts": 1788917084.61,
    "latest_ts_iso": "2026-09-09T01:24:44Z",
    "n_cached": 317,
    "oldest_ts": 1788873653.9,
    "oldest_ts_iso": "2026-09-08T13:20:53Z",
    "age_s": 371.3,
    "n_cached_avg8": 40,
    "oldest_avg8_ts": 1788873653.9
  },
  "quantities": ["amp", "phase", "real", "imag", "coh"],
  "units": { "amp": ["linear", "db", "log10"], "phase": ["deg", "rad"] },
  "refs": ["raw", "sun", "cal"]
}
```

- `quantities`/`units`/`refs` are served so the toggle bars can be built from
  the backend's own vocabulary instead of a hard-coded copy.
- `obs.age_s` is the age of the newest cached integration (the status strip's
  `vis.age_s`: amber past 600 s, stale past 1800 s); `n_cached` counts
  full-resolution `vis_full` shards (3 d retention) and `n_cached_avg8` the
  8x channel-averaged `vis_avg8` shards (60 d, eight integrations each).
- Each `vis_avg8` channel is the MEAN of its 8 native channels, and its
  frequency axis reports the mean frequency of that block (channel 0 is the
  mean of native channels 0-7, not native channel 0's own frequency) --
  `freq_top_mhz` in the shard's meta is already offset by half a block so
  `freq_axis()`'s `top - k * chan_bw_mhz` lands on each block's centre.
- `sets.live`/`sets.wired` are packet indices, ascending, matching the keys
  used everywhere else (`spectra.baselines[].i/j`, `matrix.inputs`).
- `inputs` covers the union of both sets; a packet index absent from
  `wired` never appears elsewhere in a response for the current layout.
- `obs.n_cached` is the number of integrations in the store for the current
  obs (drives the "integration N of M" framing if ever needed; not currently
  rendered, kept for parity with the SNAPs `age_s` "never blank" rule).
- Fetched once on page load and whenever `set` changes is not necessary — the
  frontend fetches this once per mount, not polled.

## `GET /api/vis/times?t0=<iso>&t1=<iso>`

Available integration timestamps in `[t0, t1]`, for the history time slider
(same pattern as the SNAPs history slider).

```json
{ "t": [1757325600, 1757325737, 1757325874], "t0": 1757325600, "t1": 1757326000,
  "n": 3, "n_raw": 3, "dt_s": 137.438953472 }
```

- `t0`/`t1` accept an ISO 8601 UTC string OR a unix timestamp; both default
  sensibly (`t1` = now, `t0` = 6 h back) and the span is clamped to 90 d.
  Anything unparseable is a 400.
- `n_raw` is how many integrations the span really holds; `t` is uniformly
  decimated above 50 000 entries.
- `t` is ascending unix seconds. May be decimated server-side for a very wide
  window; the frontend only uses it to drive a slider index, never assumes a
  fixed cadence.

## `GET /api/vis/spectra`

One or more baseline spectra at a single integration.

Query: `ts=latest|<unix>`, `set=live|wired`, `pairs=auto|cross|all`,
`quantity=amp|phase|real|imag|coh`, `units=linear|db|log10|deg|rad`,
`ref=raw|sun|cal`, `nchan=<int>` (optional, default 768, clamped to 1..3072 and
further reduced to honour `max_cells`, default 400 000).

```json
{
  "ts": 1757325874,
  "freq_mhz": [484.4, 484.3, "...nchan floats, descending"],
  "baselines": [
    {
      "i": 0,
      "j": 0,
      "ant_i": 1,
      "ant_j": 1,
      "y": [1.2, 1.3, "...same length as freq_mhz, null where flagged"]
    }
  ],
  "flags": {
    "first_of_file": false,
    "sun_below_horizon": false,
    "cal_file": "cal_sep03peak_core17.h5"
  },
  "ts_iso": "2026-09-09T01:24:44Z",
  "obs": "2026-09-04-16:43:47",
  "set": "live", "pairs": "auto",
  "quantity": "amp", "units": "db", "units_label": "dB",
  "ref": "raw", "ref_meta": { "ref": "raw" },
  "nchan": 96, "source": "mirror", "transform_s": 0.001
}
```

- `pairs=auto` returns only `i === j` (the autos view); `pairs=cross` returns
  only `i < j` pairs among the selected `set`; `pairs=all` returns both
  (mainly for the expanded cross-detail view fetching one specific `i,j`, in
  which case the frontend still uses `pairs=cross` and picks the one entry it
  wants out of the full list — there is no single-baseline query param,
  fetching the whole set at low `nchan` for the panel grid and again at full
  `nchan` for the expanded view is cheap enough per `docs/plan.md`'s measured
  read times).
- `units=deg|rad` only apply when `quantity=phase`; `linear|db|log10` apply to
  `amp|real|imag`; `coh` (coherence, crosses only) is always linear in
  `[0, 1]`. An inapplicable `units` value is a **400** (see backend note 3);
  omitting `units` picks the default for that quantity (`amp` -> `db`,
  `phase` -> `deg`, `real`/`imag`/`coh` -> `linear`).
- Channel averaging follows the quantity: `amp` averages LINEAR POWER (so a
  fringing baseline keeps its rms amplitude), `phase` and `coh` average in the
  COMPLEX plane (so an unstopped fringe correctly averages away). A `coh`
  spectrum at `nchan=3072` is therefore larger than the same one at `nchan=8`;
  that is physics, not a bug.
- `ts` must be within half an integration of a cached one; anything else is a
  404 rather than a silent nearest-neighbour match.
- `ref=sun` fringe-stops toward the Sun before computing `quantity`; `ref=cal`
  divides by the deployed cal file (`flags.cal_file` names it); `ref=raw`
  applies neither. `flags.sun_below_horizon` lets the frontend note in prose
  when a Sun-stopped reference is meaningless right now.
- `y` entries are `null` for a flagged/missing channel (RFI, dark subband),
  matching the SNAPs "null and downstream skip it" convention
  (`lib/spectrumUtils.ts`).

## `GET /api/vis/matrix`

An NxN amplitude/phase/etc. matrix at one integration, for the `matrix` view.

Query: `ts=`, `set=`, `quantity=`, `units=`, `fmin=<MHz>`, `fmax=<MHz>`
(the band average window; omitted = the whole band).

```json
{ "inputs": [0, 1, 2, 5, 9], "m": [[0.1, 0.2], [0.2, 0.05]] }
```

- `inputs` gives the packet index for each row/column, same order both axes;
  the frontend looks up antenna numbers via `/api/vis/inputs`.
- `m` is row-major, `m[i][j]` for `inputs[i], inputs[j]`; diagonal is the
  autocorrelation value in the requested quantity/units (exactly 1 for `coh`).
  The lower triangle is filled from `V_ji = conj(V_ij)`, so `phase` and `imag`
  are antisymmetric and every other quantity symmetric. A cell with no value
  (an input missing from the cal, say) is `null`.
- `ref` is accepted here too (`raw|sun|cal`), and `ts` defaults to `latest`.
- Also served: `antennas` (ant numbers per row), `band_mhz`, `n_channels`,
  `ts`/`ts_iso`, `quantity`/`units`/`units_label`/`ref`/`ref_meta`, `flags`.
  `fmin >= fmax`, or a window with no channels in it, is a 400.

## `GET /api/vis/waterfall`

Time-frequency waterfall for one baseline, for the cross-detail expanded
view.

Query: `i=<packet_idx>`, `j=<packet_idx>`, `t0=<iso>`, `t1=<iso>`,
`quantity=`, `units=`, `ref=`, `max_cells=400000` (server-side decimation,
same convention as the SNAPs history endpoint).

```json
{
  "t": [1757325600, 1757325737],
  "freq_mhz": [484.4, 484.3],
  "z": [[1.2, 1.3], [1.1, 1.4]],
  "res": "60s"
}
```

- `z[time_index][freq_index]`, i.e. one row per `t` entry, matching the
  SNAPs `SnapHistoryResponse.z_db` shape (`lib/types.ts`) so the same
  heatmap-building code path can be reused.
- `res` names the sample spacing actually served (`"137s" | "10min" | "1h" |
  "6h"`), and `stream` says which store stream it came from: `vis_full`
  (full 3072 channels) for a span up to 6 h, `vis_avg8` (384 channels, 60 d of
  history) above it, with an automatic fallback to `vis_avg8` when the
  full-resolution shards for that span have already expired.
- `i`/`j` may be given in either order; the response reports them sorted, with
  `ant_i`/`ant_j` alongside. `i == j` is allowed and gives the input's
  autocorrelation waterfall.
- Decimation is block-averaging in the complex plane (or in power for `amp`),
  never striding; `len(t) * len(freq_mhz) <= max_cells` holds strictly, and
  `max_cells` is clamped to 1 000..2 000 000. `n_samples_raw` reports how many
  integrations went in. An empty span returns empty `t`/`z` arrays, not a 404.

## `GET /api/vis/coherence`

The night coherence matrix (|V|/sqrt(A_i A_j) per baseline, averaged over a
time window), for the `coherence` view.

Query: `t0=<iso>`, `t1=<iso>`, `set=live|wired`.

```json
{ "inputs": [0, 1, 2, 5, 9], "m": [[1.0, 0.8], [0.8, 1.0]] }
```

Same row/column convention as `/api/vis/matrix`; values are linear
coherence in `[0, 1]` (diagonal is always 1 by construction and rendered as
such).

- With **no** `t0`/`t1` the window defaults to the 02:00-05:00 PT quiet window
  of the last completed night — the same window the SNAPs night median uses.
- The average is the VECTOR mean over time and frequency divided by
  `sqrt(<A_i><A_j>)`, so an uncalibrated fringe averages away: raw night
  coherence is small by construction and `ref=sun`/`ref=cal` (also accepted
  here) is what makes a source add up.
- Served from `vis_avg8` (falling back to `vis_full`); also returns `t0`/`t1`
  (+ `_iso`), `antennas`, `n_samples`, `n_channels`, `chan_avg`. A span with no
  data returns an all-`null` matrix of the right shape, never a 404.
- `t1 - t0` is capped at **7 d**; a longer span is a **400**, not a silent
  clamp (unlike most other spans in this API), because this route accumulates
  `vis_avg8` shard-by-shard rather than materialising the window, and a
  request that walks weeks of shards deserves to be visible to the caller.
- The accumulation is `sum V_ij` / `sum A_i` per baseline over the whole band,
  one shard loaded (and then dropped) at a time, so memory stays bounded by a
  single shard regardless of the span; each shard's baselines are resolved
  against ITS OWN stored input list (an older shard may carry fewer wired
  inputs), and a baseline absent from a given shard simply does not
  contribute samples from it rather than being NaN-poisoned or dropping the
  shard's other baselines.

## Server-rendered figures (M2 figures, 2026-09-08)

The Visibilities page's **default view** is a single server-rendered PNG, not
the interactive Plotly/uPlot views above (kept behind `view=interactive` in
the frontend). `casm_monitor/collectors/figures.py` renders every
`(kind, set, ref)` combination every 1800 s (`cadences.figures`) from the last
24 h of cached `vis_avg8` (falling back to `vis_full` when it is thin) into
`store_root/figures/vis/<set>/<ref>/<kind>@{1x,2x}.png` plus a
`manifest.json`; `casm_monitor/web/figures.py` serves that tree read-only.
`set` is `live|wired`; `ref` is `raw|sun|cal` (`cal` only exists once the
deployed cal resolves); `kind` is `matrix_<q>`/`spectra_<q>` for
`q` in `amp|phase|real|imag|coh`, or `autos`.

### `GET /api/figures/vis/manifest?set=&ref=`

```json
{
  "rendered_utc": "2026-09-09T02:12:00Z",
  "set": "wired",
  "ref": "raw",
  "t0": 1788839320.29,
  "t1": 1788925720.29,
  "n_integrations": 144,
  "stream": "vis_avg8",
  "obs": "2026-09-04-16:43:47",
  "files": { "matrix_amp": { "1x": "matrix_amp@1x.png", "2x": "matrix_amp@2x.png" } },
  "kinds": ["matrix_amp", "spectra_amp", "autos", "..."]
}
```

`t0`/`t1` are the span of CACHED samples actually used (not the `[now-24h,
now]` query boundary), so `t1` doubles as the collector's own
skip-when-unchanged watermark. 404 when nothing has been rendered yet for
that `set`/`ref` (e.g. `cal` before a cal file resolves).

### `GET /api/figures/vis/<set>/<ref>/<kind>@1x.png` (and `@2x.png`)

The PNG itself. `1x` is the page-default half-resolution image (matplotlib
`dpi=55`); `2x` is the full-resolution "open full size" target
(`dpi=110`, ~1.6 in per panel — a 24-input matrix is ~4200 px wide). Both are
the same figure, so a browser `srcset="<1x> 1x, <2x> 2x"` picks the right one
without a second render. Response headers: `ETag` (a 16-hex-char sha256
prefix of the file's own bytes), `Cache-Control: public, max-age=1800`
(the collector's own cadence) and `Last-Modified`; a matching
`If-None-Match` gets a bare `304`. `kind`/`suffix` are validated against the
exact whitelist the collector renders before any filesystem access — an
unknown `kind` or a path-traversal attempt is a `400`, never a directory
listing.

### `GET /api/figures/vis/list`

Which `(set, ref)` combos have at least one rendered manifest, for the
frontend to know what exists without probing every combination:

```json
{
  "kinds": ["matrix_amp", "..."],
  "sets": ["live", "wired"],
  "refs": ["raw", "sun", "cal"],
  "combos": [{ "set": "wired", "ref": "raw", "rendered_utc": "2026-09-09T02:12:00Z", "kinds": ["..."] }]
}
```

The frontend appends `?v=<rendered_utc>` (from the manifest) to every image
URL, so an unchanged render is a guaranteed browser cache hit and a new one
is a URL the browser has never seen — this is what makes toggling views feel
instant after the first paint (which also prefetches the other four
quantities' `@1x` images for the current set/ref/view in the background).

## Errors

Same convention as the SNAPs contract: non-2xx responses carry a JSON body
with a `detail` string where practical; the frontend's `lib/api.ts` surfaces
any non-2xx as a toast via `emitError` and otherwise treats a missing/empty
series as "no data" rather than an error (e.g. no cached integrations yet).
