# casm_monitor frontend

React and TypeScript with Vite, built into `../casm_monitor/web/static/`.
The backend needs no Node runtime. The current workspace uses a dark interface
and server-rendered scientific figures, with no Plotly in the reachable bundle.
Legacy plotting source files remain for reference, not as current design rules.
See [DESIGN.md](DESIGN.md) and the superseding section of
[the original plan](../docs/plan.md).

## Build and development

Reuse the available `node_modules` dependency tree when possible. A fresh
environment can use `npm install`; `.npmrc` contains this machine's npm proxy
configuration. Installing another plotting stack is not part of this workspace.

```bash
npm run build
```

This type-checks and builds the static assets. `emptyOutDir: true` replaces this
checkout's old assets; build only in the isolated workspace, not the running
production checkout. Commit the generated assets with the source. Do not commit
`node_modules`, including a local dependency-reuse symlink.

`npm run dev` starts Vite. Its existing `/api` and `/ws` proxy targets port 8060;
that default is **production**, not the isolated writable-evidence workspace.
For end-to-end workspace checks use the built application on port 8061 with the
environment flags in the [repository README](../README.md), rather than sending
new workspace requests to production. `npm run preview` alone has no backend.

## Interface and routes

Three primary tabs group the operator workflow:

- Observation: rolling 24-hour injection recovery, T1/RFI, baseline phase,
  source history, candidates and existing imaging products.
- Readiness: infrastructure, investigation queue, calibration-day comparison,
  independent Cyg A transit, manually confirmed build/review and events.
- Antennas: geometry-selected baselines and existing SNAP history.

`/observation` opens the morning-check page. `/vis` and `/antennas` use the
selected-data explorer; `/search` uses T1 scientific plots; `/snaps` uses the
bounded transmitted-band history adapter. `/cal/compare`, `/cal/transit`,
`/cal` and `/sources` expose their respective scientific workflows. Existing
candidate/event and imaging routes remain available; no other service is retired.

The scientific controls select baseline pairs, quantity, reference, date/time and
frequency interval. Rendering is explicit and bounded; no interactive Plotly
widget or whole-array/raw-data fallback runs silently. Every new scientific view
provides its image, numerical product and metadata downloads where supported.
Saved investigations copy immutable plot pixels and preserve the selected data
and processing. Workspace queue refresh retains newly seen injection misses
locally; an explicit request changes `queued` to `requested`, not `running`.
There is no agent executor or Slack integration.

T1 emitted candidate distributions and raw-peak cap warnings have different
evidence. The fork's clustered output count cannot measure its pre-clustering
10k cap. Coverage and bounded log-tail limits must stay visible. Missing data,
missing artifacts and failed requests must not become zero counts or flat spectra.

Build staging shows the recipe and antenna selection. A separate confirmation
starts only the canonical Sun calibration driver under resource bounds; it is
not a deployment approval. No SNAP acquisition, operational injection/dump,
deployment or restart-default change is enabled by workspace navigation.

## Implementation pointers

- `src/App.tsx`: current reachable routes; do not infer bundle use from legacy
  source files merely remaining in the tree.
- `src/workspace.css`: current dark tokens, layout and controls, loaded after
  the retained base stylesheet.
- `src/components/Workspace.tsx`: range controls, plot/download presentation and
  saving investigation selections.
- `src/pages/SciencePage.tsx`, `T1Page.tsx`, `ReviewPage.tsx`,
  `SnapWorkspacePage.tsx`, `CommissioningPage.tsx`, `TransitPage.tsx` and
  `SourceHistoryPage.tsx`: operator workflow pages.
- API contracts: [science](../docs/api-science.md),
  [search/review](../docs/api-review.md),
  [commissioning/source history](../docs/api-commissioning.md).

Run the repository's `scripts/check_workspace_browser.py` against the isolated
backend for desktop/mobile overflow, route, scientific-render and safe-request
checks. Browser checks are not proof of scientific usefulness or calibration
validity; compare the selected plots and their provenance with trusted workflows.
