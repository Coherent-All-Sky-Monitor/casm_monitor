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

## Layout

- `src/lib/` — `api.ts` (typed fetch client), `types.ts` (API contract
  types), `toast.ts` (error toast bus), `useStatus.ts` (WebSocket + polling
  fallback for the status strip), `useUrlParam.ts` / `timeRange.ts` (URL-backed
  toggle state), `plotly.ts` (single Plotly import point).
- `src/components/` — app shell pieces (`Clock`, `StatusStrip`, `TabNav`,
  `ToastStack`) and shared building blocks later tabs reuse (`Page`,
  `ToggleBar`, `TimeRangePicker`, `SmokeSparkline`).
- `src/pages/` — one page per tab; `PlaceholderPage` renders the M1-M5 stub
  copy from `lib/placeholderCopy.ts`, `EventsPage` is the real M0 Events tab.
