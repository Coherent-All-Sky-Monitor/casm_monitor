# Search tab API contract (M2b)

Written by the frontend agent for the backend agent implementing these routes
concurrently, per `docs/plan.md` section "4b. Search". The frontend
(`frontend/src/lib/api.ts`, `frontend/src/lib/types.ts`) is built exactly
against this; please implement to the letter or, if a field needs to change,
edit this file and ping so both sides stay in sync.

`t0`/`t1` query params are ISO 8601 UTC strings everywhere below (matching
`TimeRangePicker`'s output); response-body timestamps (`last_ts`, `t` arrays)
are unix seconds.

## `GET /api/search/summary?t0=<iso>&t1=<iso>`

The numbers behind the one-sentence section line.

```json
{
  "t0": "2026-09-08T11:00:00Z",
  "t1": "2026-09-08T12:00:00Z",
  "n_cands": 12340,
  "per_job": [
    { "job": 0, "node": "corr1", "n": 1540, "rate_per_min": 25.6, "last_ts": 1757325874 }
  ],
  "thresholds": {
    "corr1": { "snr": 15, "dm_min": 20 },
    "corr2": { "snr": 15, "dm_min": 20 }
  },
  "funnel": { "n_cands": 12340, "n_clusters": 46, "n_stored": 46, "n_vetoed": 12294, "n_triggers": 3 }
}
```

- `per_job` has one entry per hella job (0-3 corr1, 4-7 corr2, per
  `docs/plan.md`); `n` is candidates in `[t0, t1]`, `rate_per_min` is `n`
  divided by the window length in minutes (server computes this so the
  frontend never needs to know the window length in seconds itself).
- `thresholds` mirrors the status-strip `hella_corr{1,2}_{snr,dm_min}` items
  (`lib/statusSentence.ts`) so the section sentence can restate them without
  a second round trip.
- `funnel` is the same shape as `/api/search/funnel`'s totals, just summed
  over the window rather than binned, for the one-line summary.

## `GET /api/search/hist?field=snr|dm|width|beam&t0=<iso>&t1=<iso>&bins=<int>&log=0|1`

A histogram of raw (T1) candidates over the window.

```json
{ "edges": [0, 5, 10, 15], "counts": [102, 55, 12] }
```

- `edges` has `len(counts) + 1` entries. `log=1` requests log-spaced bin
  edges (meaningful for `snr`/`dm`, which span decades); the frontend passes
  `log=1` whenever the page's axis-scale control is set to `log`.
- `bins` defaults to a reasonable server-side value (30-ish) if omitted.
- `n_total`/`n_used` are exact counts over the WHOLE window (one SQL
  `GROUP BY`, not a capped sample of rows read into Python), and always equal
  each other: every candidate lands in exactly one bin (out-of-range values
  clip into the first/last bin), so a storm gulp cannot bias the shape of the
  histogram toward whichever rows happened to be read first.

## `GET /api/search/scatter?x=<field>&y=<field>&t0=<iso>&t1=<iso>&max_points=20000`

A (decimated) scatter of two fields over the window. `x`/`y` are any of
`snr`, `dm`, `width`, `beam`, `time` (unix seconds, for the DM-vs-time and
rate-style views).

```json
{ "x": [12.1, 40.2], "y": [3.4, 5.1], "n_total": 12340 }
```

- `x`/`y` arrays are the same length, a random or stratified sample of at
  most `max_points` rows from the `n_total` candidates in the window (the
  frontend does not assume any particular sampling order — it plots as
  unconnected points only, alpha 0.4, never lines).
- Sampling: every row is returned when `n_total <= max_points`; below 1e6
  rows a uniform random sample is drawn in SQL (`ORDER BY random()`, one
  pass); at or above 1e6 rows the budget is split proportionally across the
  jobs present and each job is strided independently, so one job's storm
  cannot crowd out the others' points. Either way the returned points are
  re-sorted by time before being sent, so `x`/`y` are never in a surprising
  order when `x=time`. `n_total` is always the exact row count.

## `GET /api/search/beam-map?t0=<iso>&t1=<iso>`

Per-beam candidate counts.

```json
{ "counts": [12, 0, 3, "...512 entries, beam 0..511"] }
```

- `counts[beam]`, global beam numbering 0-511 (`beam = row*32 + col` for the
  16x32 occupancy heatmap, per `docs/plan.md`'s beam layout).

## `GET /api/search/rate?t0=<iso>&t1=<iso>&step_s=60`

Candidate rate vs time, overall and per job.

```json
{
  "t": [1757325600, 1757325660],
  "per_job": { "0": [12, 15], "4": [8, 9] },
  "total": [40, 44]
}
```

- `t` is the left edge of each `step_s`-wide bin, ascending unix seconds.
  `per_job` keys are job numbers 0-7 as strings (JSON object keys); a job
  with zero candidates in the whole window may be omitted from `per_job`
  rather than sent as an all-zero array.

## `GET /api/search/funnel?t0=<iso>&t1=<iso>&step_s=600`

The T2 funnel vs time (from `gulp_stats`): candidates in, clusters found,
clusters stored (post-veto), and vetoed, each binned the same way as `rate`.

```json
{
  "t": [1757325600, 1757326200],
  "n_cands": [400, 420],
  "n_clusters": [3, 2],
  "n_stored": [3, 2],
  "n_vetoed": [397, 418]
}
```

All four arrays are the same length as `t`.

## Errors

Same convention as the SNAPs/Visibilities contracts: non-2xx responses carry
a JSON body with a `detail` string where practical; the frontend's
`lib/api.ts` surfaces any non-2xx as a toast via `emitError`. An empty window
(no candidates) is a normal 200 with zeroed/empty arrays, not an error.
