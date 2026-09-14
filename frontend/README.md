# casm_monitor frontend

React + TypeScript app (Vite), built once and committed as static assets into
`../casm_monitor/web/static/` so the backend never needs node at runtime. See
`../docs/plan.md` ("Decisions" -> Stack) for the rationale.

## Install

Node 18 (`/usr/bin/node`, v18.19.1) and npm 9.2 are available on corr1. There
is no direct internet; go through the proxy:

```sh
export http_proxy=http://10.70.0.1:8118 https_proxy=http://10.70.0.1:8118
```

`.npmrc` in this directory already sets `proxy`/`https-proxy` for npm, so a
plain install works once the shell env vars above are exported (needed for
npm's own bootstrap and for any other proxy-aware tool):

```sh
cd frontend
npm install
```

## Dev

```sh
npm run dev
```

Starts the Vite dev server. `/api` and `/ws` are proxied to
`http://127.0.0.1:8060` (see `vite.config.ts`), so run the backend
(`casm-monitor-web.service` or its dev equivalent) on that port first.

## Build

```sh
npm run build
```

Type-checks with `tsc --noEmit` then runs `vite build`. Output goes to
`../casm_monitor/web/static/` (`emptyOutDir: true`, `base: '/'`) and is the
thing that gets committed — `frontend/node_modules` and `frontend/dist` are
gitignored, `casm_monitor/web/static/*` is not.

## Preview

```sh
npm run preview
```

Serves the production build locally without a backend, for a quick sanity
check when :8060 isn't up.

## Look

`DESIGN.md` in this directory is the whole visual spec: white paper, a grid of
equal panels, tiny grey panel titles, thin signal-blue lines, viridis
waterfalls, and state written as sentences rather than badges. Tokens live in
`src/lib/theme.ts` (for Plotly) and as CSS custom properties at the top of
`src/styles.css` (for the page); keep the two copies identical.

## Layout

The default route is `/observation`. Three primary tabs organize existing
diagnostics: Observation (science overview, search, candidates and imaging),
Readiness (status, calibration products and events), and Antennas (SNAPs and
visibilities). Existing diagnostic URLs remain valid.

The science overview polls the read-only `/api/observation` endpoint every
30 seconds. It shows recorded solar products and injection outcomes, including
explicitly labelled synthetic replay links. Layout wiring, intended selection,
and inspected deployed membership are distinct; the beam selector highlights
the inspected union or a single beam. Counts never come from filenames.
The coordinate selector compares current-layout and recorded weights-product
positions; product mode omits antennas without product coordinates. Unresolved
slot identities are not highlighted as deployed. A replay thumbnail names its
own trial, which can differ from the latest completed trial.
An API failure retains the last overview with a visible stale-evidence notice.
SNAP and visibility figure manifests are refreshed every minute; their images
remain cached by render timestamp. No scientific computation is triggered by
these page refreshes.
Interactive SNAP history defaults to the last hour; the saved overview figures
retain their existing 24-hour window.

The frontend uses the aggregate API's local, UTC and LST values; the displayed
clock updates with its 30-second evidence poll. It is not a continuously ticking
instrument clock. Product timestamps and observation coverage are shown
separately from the time the browser last checked the API.

- `src/lib/` — `api.ts` (typed fetch client), `types.ts` (API contract types),
  `toast.ts` (error bus feeding the one alert sentence), `useStatus.ts`
  (WebSocket + polling fallback), `statusSentence.ts` (status payload to
  prose), `snapText.ts` (every string the SNAPs page says), `theme.ts` +
  `plotStyle.ts` (design tokens and the shared Plotly styling), `useColumns.ts`
  (grid columns, so only the bottom row and left column carry axis ticks),
  `useUrlParam.ts` / `timeRange.ts` (URL-backed control state), `plotly.ts`
  (single Plotly import point — the cartesian bundle, which is the smallest
  dist carrying the `heatmap` trace the waterfalls need).
- `src/components/` — `Header`, `StatusLine`, `AlertLine`, and the plain
  controls (`Segmented`, `TimeRangePicker`); `components/snaps/` has
  `SpectrumPanel` (one input), `BoardSection` (one board's line plus its
  panels) and `InputDetail` (the expanded view that replaces the grid).
- `src/pages/` — one page per tab; `PlaceholderPage` renders the one-sentence
  copy from `lib/placeholderCopy.ts`, `EventsPage` is the real M0 Events tab.
- `?mock=1` on the SNAPs page serves synthetic boards from `lib/mockSnaps.ts`,
  for working on the layout without the backend.
