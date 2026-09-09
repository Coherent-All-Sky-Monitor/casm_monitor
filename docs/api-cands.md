# Candidates tab API contract (M5)

Per `docs/plan.md` section "5. Candidates". This router does not reimplement
T2/T3's SQL: it imports and calls `casm_t3.web.app`'s own query/write helpers
(`q`, `q_write` via `label()`, `held_reason`, `friendly_outcome`,
`trigger_cfg`) and `casm_t2.db` / `casm_t3.web.nowpanel` /
`casm_t3.web.statsplot`, pointed at this service's own `settings.t2_db` and
`settings.candidates_dir` (which default to the same paths t3-web itself
reads: `/mnt/nvme5/casm_pipeline/db/t2.sqlite`,
`/mnt/nvme5/casm_pipeline/candidates`). See `casm_monitor/web/cands.py`'s
module docstring for why t3-web's Jinja app is never mounted directly
(absolute redirects, a write path that must stay singular).

t3-web stays up on `:8050` until this tab is verified against the live
store, then is retired (`docs/plan.md`).

All timestamps are the T2 database's own ISO-8601 strings
(`"2026-09-09T12:00:00.000"`, no `Z`, millisecond precision, UTC) unless
noted. Event names match `^\d{6}[a-zA-Z0-9]{4,6}$` (`YYMMDD` + a 4-6 char
random suffix: legacy 10-char names before 2026-07-31, 12-char since); any
other value in a `{name}` path segment is a 400. Artefact filenames served
through the plot route match `^[A-Za-z0-9_.-]+\.(png|json)$`; anything else,
or a filename that resolves outside `candidates_dir/<name>/`, is a
400/404.

## `GET /api/cands/events?tier=&tag=&view=candidates&limit=500&since=`

The events table. Mints the `casm_monitor_csrf` cookie if the browser does
not already hold one (same double-submit scheme as `docs/api-cal.md`'s
upload route; see `casm_monitor/web/cal.py`).

- `view=candidates` (default): dump attempts only — events T2 shortlisted
  **and** tried to dump (`triggers.action` triggered/refused_daemon/failed,
  or refused for disk). `view=all`: every stored (tiered) event, including
  storm-gated/suppressed ones.
- `tier`: exact match (`A`/`B`/`C`); empty = no filter.
- `tag`: substring match against the newest **label** per event (not the
  pipeline's automatic `tags` column) — same as t3-web's `tag` filter.
- `since`: an event_utc lower bound, any ISO-8601 timestamp (`Z`, an offset,
  a space separator or naive = UTC). It is parsed and canonicalised to the
  DB's own `T`-separated UTC form before the comparison — sqlite compares
  these as text and `' ' < 'T'`, so a space-form bound would otherwise select
  the wrong same-day rows. A value that is not a timestamp is a 400.
- `limit`: 1-5000, default 500.

```json
{
  "events": [
    {
      "name": "260909aabbcc",
      "event_utc": "2026-09-09T12:00:00.000",
      "snr": 30.0,
      "dm": 120.0,
      "width": 3,
      "beam": 42,
      "tier": "A",
      "tags": [],
      "n_beams": 2,
      "n_members": 5,
      "alt_deg": 45.0,
      "az_deg": 90.0,
      "label": null,
      "outcome": "dumped"
    }
  ]
}
```

`outcome` is t3-web's own one-phrase-per-row logic: `friendly_outcome()` for
an event with a trigger row (`"dumped"`, `"storm: 60 s gate"`, `"missed gulp
window (T2 too slow for ring)"`, ...), else `held_reason()` (`"RFI: too many
beams"`, `"S/N below tier B (15)"`, `"DM below 20 floor"`, ...). `label` is
the newest row in `labels` for that event, or `null`.

## `GET /api/cands/events/{name}`

One event's trigger card: the cluster row, every trigger attempt, every
label, the plot file list, the trigger's JSON meta (context.members
stripped — too big for a page, same as t3-web), and the same `data_status`
sentence t3-web computes (`"raw dump on disk"`, `"raw dump deleted by
janitor"`, `"no dump — <outcome>"`, `"no dump attempt — <held reason>"`, ...).
Mints the CSRF cookie, same as `/events`. 404 if no cluster row has that name.

```json
{
  "event": { "...every clusters column...": "..." },
  "tags_display": ["rfi_wide"],
  "triggers": [ { "action": "triggered", "detail": "ring ok", "...": "..." } ],
  "labels": [ { "label": "frb", "who": "monitor", "notes": "...", "created_utc": "..." } ],
  "plots": ["260909aabbcc.png"],
  "meta": { "...trigger json, if any...": "..." },
  "data_status": "raw dump on disk",
  "label_choices": ["frb", "pulsar", "rfi", "unsure"]
}
```

`tags_display` is the cluster's `tags` column split on commas with
`src:*` entries dropped (t3-web hides those on the web; DM-overlap source
matching mislabels RFI, humans assign labels instead — `casm_t3/web/app.py`).

## `GET /api/cands/events/{name}/plot/{fname}`

The candidate PNG or the per-event JSON meta file, straight from
`candidates_dir/<name>/<fname>`. ETag (sha256 prefix of the bytes) +
`Cache-Control: public, max-age=86400` (immutable once t3 writes it); a
304 on a matching `If-None-Match`.

## `POST /api/cands/events/{name}/label`

Body `{"label": "frb"|"pulsar"|"rfi"|"unsure", "note": "<string>"}`.

CSRF double-submit: the request must carry the `casm_monitor_csrf` cookie
(minted by `GET /api/cands/events` or `GET /api/cands/events/{name}`) echoed
in the `X-CSRF-Token` header, `hmac.compare_digest`-compared — identical
scheme to `docs/api-cal.md`'s upload route, same cookie the Calibration tab
already mints, so one browser session shares one token across tabs. Missing
or mismatched: 403. Unknown event name: 404. `label` not one of the four
values: 400.

Concurrency: t2d and t3-web write the same sqlite file, so the write runs on
a connection with `busy_timeout = 5000 ms` and is retried up to 5 times with
exponential backoff on `SQLITE_BUSY` (`database is locked`). If it is still
locked after that, the answer is a `503` with a `detail` saying so — never a
500.

The write itself calls `casm_t3.web.app.label()` directly — t3-web's own
route function, not a reimplementation — so the `label=='frb'` promotion
into the `frbs` table happens exactly as it does from `:8050`. The `who`
column is recorded as `"monitor"` so a labelling audit can tell which UI
was used; t3-web's own default is `"web"`.

```json
{
  "name": "260909aabbcc",
  "label": "frb",
  "labels": [ { "label": "frb", "who": "monitor", "notes": "...", "created_utc": "..." } ]
}
```

## `GET /api/cands/stats?hours=12|24|48|168|720&limit=60`

The funnel numbers (`docs/plan.md` "T2 funnel: candidates, clusters, stored,
triggers") as JSON, plus the live "now at OVRO" clocks/transit table
(`nowpanel.snapshot()`) and the last `limit` raw `gulp_stats` rows. An
`hours` outside the five presets falls back to 24, same as t3-web.

```json
{
  "hours": 24,
  "win_label": "24 h",
  "presets": [ { "hours": 12, "label": "12 h" }, "...": "..." ],
  "hour": { "gulps": 420, "cands": 12000, "clusters": 40, "stored": 40, "would": 3, "ms": 5.2, "cands_s": 7.9 },
  "win": { "...same shape, over the full window...": "..." },
  "now": { "epoch_ms": 1757419200000, "lst_h": 4.2, "sources": [ { "name": "Sun", "alt": 12.0, "az": 90.0, "transit": "in 1.2 h" } ] },
  "rows": [ { "gulp_utc": "...", "n_jobs": 8, "n_cands": 1000, "n_clusters": 12, "n_stored": 3, "n_would": 1, "clustering_ms": 5.5 } ]
}
```

`now` is `null` if astropy/IERS is unhappy (t3-web's `nowpanel.snapshot()`
never raises; the page renders without the clocks in that case).

## `GET /api/cands/stats/plot.png?hours=12|24|48|168|720`

The funnel PNG. **This route only serves a file; it never renders** — the
`render_figures` job's `cands` target renders one PNG per preset window with
`casm_t3.web.statsplot.render` into `store_root/figures/cands/`
(`funnel@1x.png` for the 24 h default, `funnel_<hours>h@1x.png` for the
others, plus a `manifest.json`) every 30 min. ETag +
`Cache-Control: public, max-age=60`. 404, with a `detail` saying which
window has not been rendered yet, until that job has run.

## `GET /api/cands/injections?limit=200&offset=0`

```json
{
  "injections": [ { "inject_utc": "...", "beam": 10, "dm": 50.0, "est_snr": 20.0, "rec_snr": 19.4, "gate_t1": 1, "gate_t2": 1, "event_name": null, "...": "..." } ],
  "day": { "n": 40, "t1": 38, "t2": 36, "tr": 30, "done": 40 }
}
```

`limit` is 1-2000 (default 200) and `offset` pages through older rows; both
are echoed in the response. `day` is the last-24h gate summary t3-web's `/injections` page shows
(`t1`/`t2`/`tr` are `sum(gate_t1|gate_t2|gate_trigger)`, `done` is
`count(gate_t1)`, i.e. how many have been reconciled at all).

## `GET /api/cands/frbs?limit=200&offset=0`

```json
{ "frbs": [ { "name": "260909aabbcc", "event_utc": "...", "snr": 30.0, "dm": 120.0, "width": 3, "beam": 42, "notes": "looks real", "created_utc": "..." } ], "limit": 200, "offset": 0 }
```

The FRB catalog (`frbs` table), newest first, paginated: `limit` 1-2000
(default 200), `offset` from the newest row.

## `GET /api/cands/transits`

```json
{ "snapshot": { "epoch_ms": 1757419200000, "lst_h": 4.2, "sources": [ "...": "..." ] } }
```

Same shape as `stats.now` above, its own route so the frontend's transit
sub-view can poll it without pulling the funnel numbers too.
