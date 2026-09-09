# Imaging tab API contract (M4)

Written by the frontend agent for the backend agent implementing these routes
concurrently, per `docs/plan.md` section "4. Imaging". The frontend
(`frontend/src/lib/api.ts`, `frontend/src/lib/types.ts`) is built exactly
against this; please implement to the letter or, if a field needs to change,
edit this file and ping so both sides stay in sync.

All timestamps in JSON bodies are ISO 8601 strings (UTC, e.g.
`"2026-09-09T02:41:00Z"`), unlike the Visibilities contract (unix seconds) —
the Imaging figures are already string-keyed by render, so there is no
per-sample numeric axis to keep compact.

Server-rendered, same ETag/`max-age`/`If-None-Match` behaviour as
`/api/figures/vis` (see `docs/api-vis.md` `GET /api/figures/vis/<set>/<ref>/<kind>@1x.png`
for the exact header contract): a figures collector renders PNGs/an MP4 to
disk on its own cadence and these routes serve that tree read-only, whitelist
validated (`file` against the manifest's own names, never a raw path).

## `GET /api/figures/imaging/manifest`

```json
{
  "rendered_utc": "2026-09-09T02:44:00Z",
  "cal_file": "cal_sep03peak_core17.h5",
  "antennas": [1, 2, 3, 5, 6, 8, 9, 11, 13, 15, 17, 20, 23, 26, 29, 31, 32],
  "config_fingerprint": "9f2c…",
  "latest": {
    "ts": "2026-09-09T02:41:00Z",
    "lag_s": 180.0,
    "file_1x": "latest@1x.png",
    "file_2x": "latest@2x.png"
  },
  "strip": {
    "t0": "2026-09-08T02:44:00Z",
    "t1": "2026-09-09T02:44:00Z",
    "n": 576,
    "file_1x": "strip24h@1x.png",
    "file_2x": "strip24h@2x.png"
  },
  "movie": { "file": "allsky24h.mp4", "fps": 4 },
  "sources": [
    { "name": "sun", "alt_deg": 12.3, "az_deg": 118.4, "up": true },
    { "name": "cyg-a", "alt_deg": 61.0, "az_deg": 41.9, "up": true },
    { "name": "cas-a", "alt_deg": 48.2, "az_deg": 7.6, "up": true },
    { "name": "tau-a", "alt_deg": -14.1, "az_deg": 260.3, "up": false }
  ],
  "cutouts": [
    {
      "source": "cyg-a",
      "alt_deg": 61.0,
      "az_deg": 41.9,
      "file_1x": "cutout_cyg-a@1x.png",
      "file_2x": "cutout_cyg-a@2x.png",
      "snr": 8.1,
      "ceiling_snr": 9.0
    }
  ],
  "psf_ceiling_snr": 9.0
}
```

- `antennas` is the deployed-cal antenna list (the same set the section
  sentence reports as `"<n> antennas"`); `cal_file` is the basename only, as
  used elsewhere in the monitor (e.g. `vis` tab's `ref=cal` `cal_file` flag).
- `latest` is the most recent single integration's dirty image; `strip` is a
  single server-rendered PNG that is itself a row of thumbnails spanning
  `[t0, t1]` (`n` integrations) — not `n` separate files. `movie.file` is
  `null` until the first 24 h mp4 has been rendered; `movie.fps` is served
  even when `file` is `null` so the frontend never has to hard-code it.
- `sources` is fixed order `sun, cyg-a, cas-a, tau-a`; `up` is `alt_deg > 0`
  at the horizon used for the image (the unit circle), server-computed so the
  frontend does no astronomy.
- `cutouts` is one entry per source that was more than **10 deg** above the
  horizon at the latest integration, highest first — a `image_around_source`
  cutout (`ang_max_deg=5`, `npix=51`, `grid="lm"`, `freq_avg=32`,
  `min_baseline_m=5`, bandpass-normalised) of that source at that
  integration. `snr` is the measured image SNR (`snr_info.snr`) and
  `ceiling_snr` the dirty-beam ceiling for the same geometry
  (`psf_for_result` + `compute_image_snr` on the same annulus), so the page
  can say "Cyg A: measured SNR 8.1 against a PSF ceiling of 9.0".
  `ceiling_snr` is `null` if the PSF replay failed. `cutouts` is `[]` when
  nothing is up; the files are served through the same
  `GET /api/figures/imaging/<file>` route and are whitelisted against these
  very entries.
- `psf_ceiling_snr` is the dirty-beam sidelobe SNR ceiling for the deployed
  array configuration — the `ceiling_snr` of the highest cutout this render
  computed — `null` when nothing was up or the PSF was not computed for this
  render. The PSF cost is measured on the first pass that computes one; if it
  exceeds 120 s the ceilings are recomputed only every 6th pass and the cached
  values (per configuration fingerprint) are reported in between.
- `latest.lag_s` is `rendered time − latest integration`, in seconds, so the
  page can say "latest image is 3.2 h behind" without arithmetic on two
  timestamps. `config_fingerprint` is the sha256 identity of what the frames
  behind these products were imaged with (deployed cal path + its md5, the
  deployed antenna list, `npix`, `freq_avg`, `min_baseline_m`, estimator);
  cached frames carrying any other fingerprint are expired rather than mixed
  into `latest`/`strip`/`movie`.
- 404 (no body required beyond the usual `detail` string) when nothing has
  been rendered yet at all.
- Same cache headers as `/api/figures/vis/manifest`: no special caching on
  the manifest itself beyond the browser's default, so the frontend can poll
  it (every 30 s) to notice a new render.

## `GET /api/figures/imaging/<file>`

The PNG or MP4 itself — `file` is exactly one of the manifest's own
`file_1x`/`file_2x`/`movie.file`/`cutouts[].file_1x`/`cutouts[].file_2x`
strings, or a history frame's own `file_1x` (see below), never an arbitrary
path. `1x` is the page-default
half-resolution image; `2x` is the full-resolution "open full size" target,
same `srcset="<1x> 1x, <2x> 2x"` convention as the Visibilities figures.
Response headers: `ETag` (a 16-hex-char sha256 prefix of the file's own
bytes), `Cache-Control: public, max-age=1800` and `Last-Modified`; a matching
`If-None-Match` gets a bare `304`. An unknown `file` or a path-traversal
attempt is a `400`, never a directory listing — validated against the
whitelist the collector actually rendered (the current manifest's files plus
the history route's `frames/*` tree), not a glob of the filesystem.

The frontend appends `?v=<rendered_utc>` (from the manifest, or a history
frame's own `ts` in the scrub view) to every image URL, so an unchanged
render is a guaranteed browser cache hit and a new one is a URL the browser
has never seen.

## `GET /api/imaging/history?t0=&t1=`

One frame per integration actually rendered in `[t0, t1]` (ISO strings), for
the scrub view's range slider:

```json
{
  "frames": [
    { "ts": "2026-09-08T02:44:00Z", "file_1x": "frames/1788835440@1x.png" },
    { "ts": "2026-09-08T02:46:30Z", "file_1x": "frames/1788835590@1x.png" }
  ]
}
```

- `file_1x` is `frames/<unix>@1x.png`, served under the same
  `GET /api/figures/imaging/<file>` route as the manifest's own files (the
  `/` in `file` is a normal path segment, not a query boundary) — there is no
  separate `@2x` for history frames, scrub is a browsing tool, not the "open
  full size" target.
- Only the file that actually exists on disk for that integration is listed;
  an integration whose PNG failed to render (or predates the collector) is
  simply absent rather than a broken link.
- Empty `frames: []` (not a 404) for a window with no cached integrations —
  same "empty series is not an error" convention as
  `docs/api-vis.md`/`docs/api-search.md`.

## Errors

Same convention as the other tabs' contracts: non-2xx responses carry a JSON
body with a `detail` string where practical; the frontend's `lib/api.ts`
surfaces any non-2xx as a toast via `emitError` and additionally renders the
manifest 404 inline as "no figures rendered yet" prose (same treatment
`VisFigureView`/`SnapFigureView` give a missing manifest). An empty
`history` result is not an error at all — it is rendered inline as "no
integrations in this window".
