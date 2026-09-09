# Calibration tab API contract (M3)

Written by the frontend agent for the backend agent implementing these routes
concurrently, per `docs/plan.md`. The frontend (`frontend/src/lib/api.ts`,
`frontend/src/lib/types.ts`) is built exactly against this; please implement
to the letter or, if a field needs to change, edit this file and ping so both
sides stay in sync. Until the real routes answer, the frontend runs against
`frontend/src/lib/mockCal.ts` behind the `?mock=1` URL flag; the real fetch
path (`lib/api.ts`) is the default.

All timestamps are ISO 8601 strings (UTC, e.g. `"2026-09-08T19:50:00Z"`).

## `GET /api/cal/defaults?date=YYYY-MM-DD`

Today's (or the given date's) proposed solve parameters, computed server-side
from the Sun ephemeris, the deployed layout and last night's static window.

```json
{
  "date": "2026-09-08",
  "source": "sun",
  "sources": [
    { "name": "sun", "enabled": true },
    { "name": "cyga", "enabled": false },
    { "name": "casa", "enabled": false }
  ],
  "sun_max_utc": "2026-09-08T19:50:00Z",
  "source_window": ["2026-09-08T19:20:00Z", "2026-09-08T20:20:00Z"],
  "window_offset_min": 30,
  "static_window": ["2026-09-08T03:00:00Z", "2026-09-08T03:30:00Z"],
  "static_note": "last night, 03:00 to 03:30 UTC",
  "antennas": [1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18],
  "antennas_note": "17 antennas from the layout",
  "ref_ant": 9,
  "tag": "20260908_1950",
  "deployed": {
    "cal_file": "/data/casm/cal/20260807_cal.h5",
    "weights_file": "/data/casm/default_weights_64ant_512beam/weights.h5",
    "scale": 128,
    "ib_scale": 512,
    "product_id": "20260807_1720"
  },
  "layout": {
    "path": "/home/user/antenna_layouts/current",
    "sha256": "a12aaaaeffdf...",
    "n_bf": 17,
    "n_wired": 24
  }
}
```

- `sources[]` lists every source the backend knows how to solve for; only
  `enabled: true` entries are selectable in the frontend's source segmented
  control, the rest render muted with "not yet".
- `source_window`/`static_window` are the prefilled defaults for the form
  below; `static_window` may be `null` (no usable static window found for
  the date), in which case the form's static control defaults to "none" and
  `static_note` explains why (e.g. "no static window found for this date").
- `antennas` are the antenna numbers currently in the beamforming set (from
  the deployed layout), used as the default "on" toggles.
- `tag` is the server's proposed build tag (frontend prefills the tag field
  with it, editable).

## `POST /api/cal/build`

Body:

```json
{
  "source": "sun",
  "source_window": ["2026-09-08T19:20:00Z", "2026-09-08T20:20:00Z"],
  "static_window": ["2026-09-08T03:00:00Z", "2026-09-08T03:30:00Z"],
  "antennas": [1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18],
  "ref_ant": 9,
  "tag": "20260908_1950"
}
```

`static_window` may be `null` (source-only solve). Response `200
{"job_id": 123, "tag": "20260908_1950"}`. This queues
`python -m bf_weights_generator.make_cal_and_weights` server-side (the ONLY
recipe entry point, casm-wiki `weights-and-deploy.md`) with these
parameters; the frontend never constructs that command itself.

Errors: `409 {"detail": "tag '20260908_1950' already exists"}` or
`409 {"detail": "a build is already running (tag '...')"}`. The frontend
shows the 409 detail inline and does not retry automatically.

## `GET /api/cal/builds`

```json
{
  "builds": [
    {
      "tag": "20260908_1950",
      "state": "done",
      "created": "2026-09-08T19:52:00Z",
      "source": "sun",
      "source_window": ["2026-09-08T19:20:00Z", "2026-09-08T20:20:00Z"],
      "n_ant": 17,
      "rank1_median": 0.94,
      "has_weights": true,
      "staged": true,
      "uploaded": false
    }
  ]
}
```

- `state` is one of `"queued" | "running" | "done" | "failed"`.
- `rank1_median` is `null` while `state != "done"` or if the solve failed to
  produce a summary. The frontend always renders it with the caption "solve
  quality, not beam quality" next to the column header, per
  `sun-transit-svd.md`'s rank1-metric caveat.
- List is expected newest-first; the frontend does not re-sort, so please
  keep that order server-side.

## `GET /api/cal/builds/{tag}`

```json
{
  "tag": "20260908_1950",
  "state": "done",
  "params": {
    "source": "sun",
    "source_window": ["2026-09-08T19:20:00Z", "2026-09-08T20:20:00Z"],
    "static_window": ["2026-09-08T03:00:00Z", "2026-09-08T03:30:00Z"],
    "antennas": [1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18],
    "ref_ant": 9
  },
  "summary": {
    "cal_h5": "/mnt/nvme5/casm_pipeline/cal/20260908_1950/cal.h5",
    "weights_h5": "/mnt/nvme5/casm_pipeline/cal/20260908_1950/weights.h5",
    "ib_h5": "/mnt/nvme5/casm_pipeline/cal/20260908_1950/ib_weights.h5",
    "rank1_median": 0.94,
    "subband_occupancy": [1.0, 1.0, 0.98, 1.0, 0.91, 1.0],
    "pointing_fit": { "az_off_deg": 0.02, "el_off_deg": -0.01 },
    "delay_fit_rms_deg": 3.2,
    "beam_check": { "peak_snr": 42.1, "fwhm_min": 0.9 },
    "figs": [
      { "name": "phase_raw_sawtooth", "title": "raw phase, sawtooth (pre fringe-stop)" },
      { "name": "phase_stage2_fringe_stopped", "title": "phase after fringe-stopping" },
      { "name": "phase_stage3_calibrated", "title": "phase after calibration" },
      { "name": "gain_delay_fits", "title": "per-antenna gain/delay fits" },
      { "name": "svd_vs_freq", "title": "SVD singular values vs frequency" },
      { "name": "rank1_vs_freq", "title": "rank-1 fraction vs frequency" },
      { "name": "beam_check_sun", "title": "beam check: sun" },
      { "name": "cal_diff", "title": "diff vs the currently deployed cal" },
      { "name": "beam_grid", "title": "beam grid" },
      { "name": "source_transit", "title": "source transit" },
      { "name": "autocorr", "title": "autocorrelations" }
    ],
    "notebook": true,
    "wall_s": 143.2,
    "peak_rss_mb": 8210
  },
  "stage": {
    "staged_utc": "2026-09-08T19:55:00Z",
    "files": [
      { "name": "weights.h5", "md5": "9f8c...", "scale": 128 },
      { "name": "ib_weights.h5", "md5": "1ab2...", "scale": 512 }
    ],
    "command": "scp weights.h5 ib_weights.h5 user@zapdos:/data/casm/staged/20260908_1950/",
    "checks": [
      { "name": "shape matches deployed layout", "ok": true, "detail": "17x512" },
      { "name": "scale within [64, 512]", "ok": true, "detail": "128" }
    ]
  },
  "uploads": [
    {
      "ts": "2026-09-08T20:05:00Z",
      "exit_code": 0,
      "command": "casm-deploy-weights --tag 20260908_1950",
      "note": "routine nightly solve",
      "registry_id": "20260908_2005"
    }
  ]
}
```

- `summary`/`stage` are `null` before the build has produced them (e.g.
  `state: "queued"` -> both `null`; `state: "running"` -> `summary: null`,
  `stage: null`; `state: "failed"` -> `summary` may still be partially
  populated with whatever the failed run produced, `figs` reflects only
  files that actually exist on disk).
- `figs[].name` is the exact stem the frontend requests at
  `GET /api/cal/builds/{tag}/figs/{name}.png`; only send entries that exist.
  The frontend renders them in the fixed order in the spec (this file's
  listing above), skipping any name not present, and any unrecognized name is
  appended at the end so nothing silently disappears.
- `uploads` is the append-only audit trail for this tag (empty array, not
  `null`, when nothing has been uploaded yet).

## `GET /api/cal/builds/{tag}/figs/{name}.png`

The rendered PNG for one figure (`name` matches `summary.figs[].name`,
without extension). 404 if not rendered.

## `GET /api/cal/builds/{tag}/log`

`200 text/plain` — the build's stdout/stderr log, newest content at the
bottom (frontend renders as-is in a scrollable preformatted block, does not
tail-poll it after the build reaches a terminal state).

## `GET /api/cal/builds/{tag}/notebook`

Downloads the diagnostics notebook the recipe wrote for this build
(`bf_weights_generator` always writes one, canonical-recipe.md). `200
application/octet-stream` (or the notebook mimetype), `Content-Disposition:
attachment`. 404 if `summary.notebook` is false.

## `POST /api/cal/builds/{tag}/stage`

No body. Queues a dry-run stage (compute md5s, run the shape/scale checks,
write the would-run command, but does not touch the deployed product).
Response `200 {"job_id": 123}`. Poll `GET /api/jobs/{id}`, then re-fetch
`GET /api/cal/builds/{tag}` for the populated `stage` block. 409 if the tag
has no `summary` yet (build not done) or a stage/upload job is already
running for this tag.

## `POST /api/cal/builds/{tag}/upload`

Body: `{"confirm_tag": "20260908_1950", "save_defaults": true, "note": "routine nightly solve"}`

- `confirm_tag` must equal `tag` in the URL (belt-and-suspenders on top of
  the frontend's own match-before-enable check). `400
  {"detail": "confirm_tag does not match"}` otherwise.
- `save_defaults`: if true, the backend also updates the ledger row
  (`GET /api/cal/status` -> `ledger_row`) so this becomes the recorded
  default cal/weights for future reads.
- Response `200 {"job_id": 123}` on success.
- `403 {"detail": "uploads are disabled on this service (CASM_MONITOR_ALLOW_UPLOAD)"}`
  when the service was started without `CASM_MONITOR_ALLOW_UPLOAD=1`. The
  frontend never shows the upload form as clickable in this state (see
  `GET /api/cal/status`) but still handles a stray 403 defensively.
- `409 {"detail": "tag '20260908_1950' has not been staged"}` if
  `stage` is `null`/absent for this tag.
- The backend must refuse (`409`) an upload while `casm_track_running` is
  true (`GET /api/cal/status`); the frontend disables the button in that
  case too, this is belt-and-suspenders.

## `GET /api/cal/status`

Polled alongside the page (no dedicated websocket channel; reuses the
existing `/api/jobs/{id}` poll for any in-flight cal job).

```json
{
  "allow_upload": true,
  "casm_track_running": false,
  "ledger_row": {
    "date": "2026-08-07",
    "weights_file": "/data/casm/default_weights_64ant_512beam/weights.h5",
    "cal_file": "/data/casm/cal/20260807_cal.h5",
    "scale": 128,
    "ib_scale": 512
  },
  "active_job": { "id": 123, "kind": "cal_build", "state": "running" }
}
```

- `allow_upload` mirrors the `CASM_MONITOR_ALLOW_UPLOAD` env var on the jobs
  unit; when false the frontend shows the disabled sentence+button instead
  of the upload form, and disables the "Run solve" -> upload chain's final
  step everywhere (build/stage remain available; only upload is gated).
- `casm_track_running` reports whether `casm-track`/LMC has an active
  observation using the currently deployed weights (medusa-lmc-control);
  when true the frontend shows an alert sentence
  ("casm-track is running; wait before uploading new weights.") and disables
  the upload button regardless of `allow_upload`/staged state.
- `active_job` is `null` when nothing cal-related is queued/running; when
  present the frontend polls `GET /api/jobs/{id}` with that id to drive any
  inline "state:" sentence on the New solve section if the user reloaded
  mid-build.

## `GET /api/jobs/{id}`

Already specced at M0/M1; used here to poll `cal_build`, `cal_stage` and
`cal_upload` jobs. Frontend only depends on `state` being one of
`"queued"|"running"|"done"|"failed"|"cancelled"` and `id`/`kind` being
present.

## Notes for the backend implementer

- Every write action (`build`, `stage`, `upload`) is a queued job, not a
  synchronous call, so a slow `bf_weights_generator` run never blocks the
  request; the frontend always polls `GET /api/jobs/{id}` to completion, then
  re-fetches the relevant `GET /api/cal/builds/...` route rather than trusting
  job `detail` payloads for the final state.
- Antenna numbers throughout this contract are ANTENNA numbers (1-indexed,
  physical), never `packet_idx`/CB-axis indices — see the vis-beamforming
  trap in casm-wiki `vis-beamforming-conventions.md`. `layout.n_bf`/`n_wired`
  and the `antennas` array in `/defaults` are all antenna-number space.
- Please keep `figs[].name` values matching the fixed vocabulary in this
  file's `GET /api/cal/builds/{tag}` example (`phase_raw_sawtooth`,
  `phase_stage2_fringe_stopped`, `phase_stage3_calibrated`,
  `gain_delay_fits`, `svd_vs_freq`, `rank1_vs_freq`, `beam_check_*`,
  `cal_diff`, `beam_grid`, `source_transit`, `autocorr`) so the frontend's
  fixed section order (spec, "Build page") renders as intended; any other
  name is still shown, just appended after the known ones.

---

## Backend notes (implemented 2026-09-09, `casm_monitor/web/cal.py`)

The routes above are live. Differences from the contract, all deliberate:

- **Timestamps.** Every timestamp the API emits is ISO-8601 Z, as specced.
  `POST /api/cal/build` accepts either that or the driver's
  `"YYYY-MM-DD HH:MM:SS"` and normalises before `RecipeParams` sees it, so
  the `/defaults` payload round-trips unchanged.
- **`tag`.** The proposed tag is `cal_<YYYYMMDD>_<HHMM>` (the HHMM of the
  altitude maximum), not `<YYYYMMDD>_<HHMM>`: it is also the build's directory
  name under `store_root/cal_builds/` and must not start with a digit-only
  token that reads as a date on its own. Any `[A-Za-z0-9][A-Za-z0-9_.-]*` is
  accepted, and a tag that already has a directory gets a 409.
- **`/defaults` extras.** `sun_max_utc` and `window_offset_min` are there as
  specced; `alt_max_utc`, `alt_max_deg`, `window_half_min`,
  `window_center_offset_min` (0.0: the window is centred ON the maximum),
  `static_note`, `antennas_note`, `prev_cal_path`, `grid_mode` (`"exact"`,
  fixed), `n_beams` (512, fixed) and `deployed.ib_file` are additions. Source
  names are the catalog spellings `sun`, `cyg-a`, `cas-a`, `tau-a`, `vir-a`
  (hyphens, not `cyga`).
- **`deployed.scale` / `ib_scale`** are parsed out of the last row of
  `deployed_weights.csv` (today 8064 and 32, the Route Z pairing), never
  defaulted. A row whose pairing cannot be read fails the stage job with that
  message rather than guessing.
- **`rank1_median`** is the driver's full-band rank-1 median (about 4-9 on
  recent solves), not a 0-1 fraction. The in-band number and the no-static A/B
  leg are in `summary.numbers.rank1_medians`.
- **`summary`** is the build's own `summary.json` plus the flat fields this
  contract asks for (`cal_h5`, `weights_h5`, `ib_h5`, `notebook`, `figs`,
  `subband_occupancy`, `pointing_fit`, `delay_fit_rms_deg`, `beam_check`).
  `pointing_fit` is the driver's per-beam table (a list of rows with
  `stated_alt/az`, `fit_alt/az`, `coh_fit`, `coh_stated`, `dalt`, `daz`), not a
  single az/el offset. `figs[]` carries `{name, file, title}`; the fringe
  diagnostics keep their relative path (`fringe_<tag>/fringe_diag_snap0_to_1.png`)
  as their name. `GET .../figs/{name}.png` accepts either the stem or the
  recorded filename.
- **`ib_h5` is null on a fresh build.** The canonical driver builds the CB
  product only; the IB mask is a companion file (`gen_ib_from_cb.py`). The
  stage pairs the build with the IB mask named by the ledger row and REJECTS
  the pair when its populated slot count differs from the CB antenna set, so a
  new antenna set needs a new IB mask before it can be deployed.
- **`stage`** is `stage.json` plus `files[]` and `command`. `checks[]` includes
  `dry_run_exit_code`, `cb_dada_files`, `cb_format_type`, `cb_populated_slots`,
  `ib_format_type`, `ib_populated_slots`. When any check fails the stage JOB
  fails (the tab should show `stage.checks` and treat `checks_ok: false` as
  "not staged"): `staged` in the build list is true only when the checks passed.
- **`save_defaults`** adds `--save-defaults` to the deploy command, which
  refreshes `/data/casm/default_weights_64ant_512beam/` on both nodes. It does
  NOT edit `deployed_weights.csv`: that ledger lives in casm-wiki and is a
  human commit.
- **Upload refusals.** 403 (flag off), 400 (`confirm_tag` mismatch), 409 (not
  staged, stale md5s vs the dry run, `casm-track` running, SCALE pairing changed
  since staging, antenna layout changed since the build, stage checks failed).
  The job re-checks all of them before it runs anything.
- **`uploads[]`** rows carry `ts`, `exit_code`, `command` (string),
  `note`, `product_id`/`registry_id`, `md5s`, `scale`, `ib_scale`,
  `save_defaults`, `output_tail`.
