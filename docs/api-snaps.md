# SNAPs tab API contract (M1)

Written by the frontend agent for the backend agent implementing these routes
concurrently, per `docs/plan.md` section "1. SNAPs". The frontend
(`frontend/src/lib/api.ts`, `frontend/src/lib/types.ts`) is built exactly
against this; please implement to the letter or, if a field needs to change,
edit this file and ping so both sides stay in sync.

All timestamps are ISO 8601 strings (UTC, e.g. `"2026-09-08T12:00:00Z"`).
`age_s` fields are seconds (float ok), server-computed at response time so the
frontend never needs client/server clock sync for staleness.

## `GET /api/snaps/boards`

Static-ish board/input inventory (re-read from `antenna_layouts/current` etc.
by the backend; cheap to poll occasionally, not on every render).

```json
{
  "boards": [
    {
      "ip": "192.168.120.52",
      "feng_id": 0,
      "slot": "A",
      "role": "antenna",
      "inputs": [
        {
          "adc": 0,
          "packet_idx": 12,
          "antenna": 13,
          "station": "N21E1",
          "in_bf": true,
          "functional": true
        }
      ]
    },
    {
      "ip": "192.168.120.59",
      "feng_id": null,
      "slot": "?",
      "role": "relay"
    }
  ]
}
```

- `role` is `"antenna"` or `"relay"`. Relay boards have **no `inputs` key**
  (frontend treats an absent/undefined `inputs` as "relay, PPS-only card").
- Antenna boards always have exactly 12 `inputs` (one per ADC channel 0..11),
  in ADC order.
- `packet_idx`/`antenna`/`station` are `null` for an unwired ADC channel (no
  row at all for that `(snap, adc)` in the layout).
- `in_bf` = included in the live beamforming set; `functional` = wired/live
  in the current layout (may be true with `in_bf` false).
- A row that exists in the layout but is marked non-functional (a gated/dead
  feed) keeps its layout `packet_idx` and `station` — the correlator input
  index does not change when a feed is gated — but reports `antenna: null`
  and `functional: false`, so the frontend dims the tile without losing the
  Kafka-row lookup key.
- Both `/api/snaps/boards` (`board_table()`) and the `snap_read` job's input
  tagging (`snapmap.read_layout_inputs`) read the SAME layout file: the
  `current` symlink (config key `snap.layout_csv`), via one shared CSV-parsing
  routine (`casm_monitor.collectors.rowmap.read_layout`).
- Expected board order for the antenna cards: feng_id 0..3 (ips `.52 .51 .62
  .73`); the frontend sorts by `feng_id` client-side but expects exactly 4
  antenna boards and 3 relay boards for the base layout.

## `GET /api/snaps/live?ip=<ip>&nchan=<int>`

Latest correlator-side (Kafka) bandpass for all 12 ADC inputs of one board.

```json
{
  "ts": "2026-09-08T12:00:00Z",
  "age_s": 8.4,
  "freq_mhz": [484.4, 484.3, "...3072 or nchan floats, descending"],
  "subbands_ok": [true, true, true, false, true, true],
  "inputs": [
    {
      "adc": 0,
      "packet_idx": 12,
      "bp": [1.2, 1.3, "...same length as freq_mhz, or null"],
      "mapping": "formula"
    }
  ],
  "eq_epoch": "2026-09-02T00:00:00Z"
}
```

- `nchan` is optional; omitted or `3072` returns full resolution. Any other
  value (e.g. `384`, `768`) asks the server to return a channel-averaged
  version at that length (server-side decimation — the frontend does not
  downsample the `live` full-res response itself, though it may further
  downsample client-side for small card plots if the server ever returns
  more than it asked for). The frontend requests `nchan=768` for the 3x4
  card grid and omits `nchan` (full res) for the expanded per-input panel.
- `subbands_ok` has exactly 6 entries, subbands 0..5 (centre MHz 476.3, 460.6,
  445.3, 429.7, 414.1, 398.4), independent of `nchan`.
- `bp` is `null` when this input has no recent Kafka data (dead/unwired).
  `mapping` is one of (2026-09-08, see `docs/notes/kafka-bandpass-schema.md`):
  `"formula"` (the primary `row = 2 * packet_idx` assignment, not yet
  validated against the measured bandpass shape, or the vis file was
  unavailable when it last ran), `"formula+verified"` (a daily/obs-restart
  validation pass confirms the formula row's shape best matches this input),
  `"mismatch"` (that validation disagrees — an operator-visible flag, not a
  fallback: the tile still shows the formula row's data), or `"unmapped"`
  when `packet_idx` itself is `null`/not a wired input (frontend renders the
  tile dimmed for `unmapped`, and with a red badge for `mismatch`).
- If the board has never been seen on Kafka, return `ts: null, age_s: null`
  and all `bp: null` (not a 404) so the card can render an empty state.

## `GET /api/snaps/board-read?ip=<ip>`

Latest board-side read via zapdos (may be old; only refreshed hourly or on
click).

```json
{
  "ts": "2026-09-08T11:03:00Z",
  "age_s": 2580,
  "freq_mhz": [500.0, 499.97, "...4096 floats, 500->375, descending"],
  "spectra": [[4096], "...12 arrays total, one per ADC"],
  "adc_rms": [12.3, "...12 floats"],
  "adc_mean": [0.1, "...12 floats"],
  "adc_gain": [8, 16, 32, 25, "...12 ints, or null if unread"],
  "eq_epoch": "2026-09-02T00:00:00Z",
  "feng_id_hw": 0,
  "feng_id_cfg": 0,
  "pps": { "ok": true, "period": 1, "detail": "locked" },
  "programmed": true
}
```

- `ts: null` (and `age_s: null`) means this board has never been read since
  the service started; the frontend shows "never read" rather than an age.
- Relay boards only ever populate `pps`/`programmed`/`ts`/`age_s`
  (`freq_mhz`/`spectra`/`adc_*`/`feng_id_*` may be `null`/absent) — the
  frontend's relay card only reads those four fields.
- `feng_id_hw` comes from the packetizer BRAM read; `feng_id_cfg` is the
  configured value; a mismatch is a caller concern (not rendered specially
  in M1, but keep both so it can be later).

Implementation notes from the board-read half (2026-09-08, measured against the
live boards — treat these as amendments to the shape above):

- `eq_epoch` is **not** a timestamp: it is a 12-hex-digit sha256 prefix over
  the board's 12x512 EQ coefficients plus its FFT shift (e.g. `"a12aaaaeffdf"`),
  which is what the store compares to raise `eq_changed`. Render it as an
  opaque tag; the *time* the epoch changed is on the Events timeline.
- `adc_gain` is always `null`: `casm_f` exposes no ADC coarse-gain getter, and
  no write-capable call may be used to discover it.
- `pps.period` is in FPGA/ADC clock ticks (250 MHz), so a healthy board reads
  ~249,998,690, and `pps.detail` is a human string
  (`count_pps=..., count_ext=..., period_pps=..., clk=250 MHz[, loopback on]`).
- The three relay boards answer *every* sync call and report `programmed: true`,
  but their autocorrelator is not accumulating (`acc_len = 0`), so they return
  `freq_mhz: null` / `spectra: null` while `adc_rms`/`adc_mean` do come back
  (from the noise generator, all 12 identical). `pps` and `programmed` are the
  fields to trust on a relay card.
- `ts` is the board's own read timestamp, so the seven boards of one pass have
  timestamps a few seconds apart rather than one shared value.

## `POST /api/snaps/board-read`

Body: `{"ips": ["192.168.120.52", "..."] }` or `{"ips": null}` (null/omitted
= all boards, both antenna and relay).

Success (job accepted): `200 {"job_id": 123}`.

Rate-limited / locked refusal: `429 {"detail": "...human-readable...",
"retry_after_s": 240}`. The frontend disables the "Read boards now" button(s)
and shows a countdown from `retry_after_s` on a 429; it does not retry
automatically, the user can click again once the countdown reaches 0.

## `GET /api/snaps/history?packet_idx=<int>&t0=<iso>&t1=<iso>&source=kafka|board&max_cells=<int>`

```json
{
  "t": [1757332800, 1757332810, "...unix seconds"],
  "freq_mhz": [484.4, 484.3, "..."],
  "z_db": [[1.2, 1.3, "...len(freq_mhz)"], "...len(t) rows"],
  "res": "10s"
}
```

- One input (`packet_idx`) at a time, per `source`.
- `max_cells` (default `1000000`) upper-bounds `len(t) * len(freq_mhz)`; the
  server decimates time and/or frequency to stay under it and reports which
  resolution it picked in `res` (one of `"10s" | "60s" | "10min" | "1h"`).
  The frontend fetches the whole `[t0, t1]` window once per selected board
  (looping this call over that board's up-to-12 `packet_idx` values) and
  scrubs the returned frames client-side with a time slider — it does not
  re-fetch per slider tick.
- `z_db` is already in dB (frontend does not re-convert unless the units
  toggle is `linear`, in which case it converts client-side with
  `10**(z_db/10)`).

## `GET /api/snaps/trend?packet_idx=<int>&t0=<iso>&t1=<iso>`

```json
{
  "t": [1757332800, "..."],
  "night_median_db": [-3.1, "..."],
  "band_power_db": [-2.8, "..."],
  "epochs": [
    { "ts": "2026-09-02T00:00:00Z", "kind": "eq", "label": "EQ coeffs reloaded" }
  ]
}
```

- `epochs[].kind` is one of `"eq" | "adc_gain" | "obs_restart"`; the frontend
  draws each as a vertical line on the trend plot with `label` as hover text.

## `GET /api/jobs/{id}`

Already specced at M0; the SNAPs tab polls this (every ~2 s) after a
`POST /api/snaps/board-read` to know when to stop showing "reading..." and
refresh the board layer. Frontend only depends on `state` being one of
`"queued"|"running"|"done"|"failed"|"cancelled"` and `id`/`kind` being
present; any extra fields are ignored.

## Notes for the backend implementer

- Every numeric array field may legitimately be `null` at the whole-array
  level (never-collected) rather than an array of `null`s — the frontend
  checks for a `null` array first, then treats an all-`null`/all-`NaN`
  contents as a flat/dead trace for the diagnosis legend.
- Frequencies are always returned descending (500→375 or 484.4→390.6-ish);
  the frontend renders whatever `freq_mhz` it is given as the y-axis order
  for waterfalls without re-sorting, per `docs/plan.md`'s explicit
  descending-band note — please do not send ascending arrays.
