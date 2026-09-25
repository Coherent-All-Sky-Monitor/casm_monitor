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

Navigation uses larger, semibold high-contrast labels on dark-blue tab surfaces.
The selected tab has a brighter blue fill, border and inset underline; keyboard
focus has a separate visible outline. Tabs wrap on narrow screens with at least
44-pixel touch targets. Route names and scientific behavior are unchanged.

One main navigation row contains Overview, Search (T1), Visibilities, SNAPs,
Source history, Candidates and Calibration. The top-right tab group is removed;
Readiness is absent from navigation, with its existing URL and status links
retained. Calibration subroutes retain the Calibration active tab. The larger
telescope title sits above the row. Overview has an enlarged heading,
observation identifier and separate aligned local, UTC and local-sidereal clock
panels, responsive on phones. The location label is Big Pine, California.

`/observation` is **Overview**: live status cards, an injection-outcome timeline,
current array/calibration context, one stream-count heatmap and one raw
reference-baseline dynamic spectrum. White panels use dark labels and prominent
status bands. History shares one interval; live cards remain marked Now.
The trial-history/seven-day table disclosure is removed. Clicking the injection
timeline opens the shared Fit/zoom/pan viewer; trial details remain selectable
inside it. All displayed data figures on reachable routes use that viewer,
including candidate statistics, saved review evidence and Calibration galleries.
Array maps retain their selection actions rather than opening image zoom.
`/vis` opens the array inspector with the current recorded beamforming set
selected, using the same inspected payload membership as Overview and SNAPs.
Green identifies beamforming inputs, blue other wired inputs; dashed map
borders identify hidden panels. Manual selections survive refresh, while the
Beamforming preset follows membership updates. Unknown membership stays unknown.
`/snaps` opens white full-band spectrum panels in compact SNAP order, with
station order/grid, a prominent all-12-ADC switch and expanded 30-day power trends.
Its default and green borders follow inspected nonzero slots in the latest
recorded deployed CB weights, not layout intent. All wired inputs remain in
the nearby layout and Overview map. SNAP status/read-details include board IPs.
`/cands` shows 12 recent saved plots by default (6/12/24/48 selectable), a compact
searchable right sidebar with show/hide toggles, and the shared image zoom.
All spectrum panels share 374.9–500.1 MHz and a full-data power scale. The default
display is dB re 10⁻¹⁰ native power units. Scale/reference/history controls are
removed; collection and the saved-history API are unchanged. Power limits cover
every finite displayed channel with padding, including narrow peaks. They update
with each saved spectrum or selection change; compact and expanded views match.
SNAPs and Overview show independent Streaming/PPS boxes from saved evidence.
`/antennas` retains the legacy URL but no longer appears as a SNAP submenu.
See [SNAP views and acquisition status](../docs/snap-spectra.md); hourly collector
activation remains separate from the preview build.
`/search` is **Search (T1)**, with white Matplotlib figures, labelled count
scales and the shared click-to-zoom viewer. Zoom pins the image and interval;
it does not query or rebin data. Stream cards wrap on smaller screens.
`/cal/compare`, `/cal/transit`,
`/cal` and `/sources` expose their respective scientific workflows. Existing
candidate/event routes remain available; no other service is retired. The old
`/imaging` frontend route is removed; scientific code and backend artifacts remain untouched.

The scientific controls select baseline pairs, quantity, reference, date/time and
frequency interval. Cached overview/explorer plots load automatically and refresh
every two minutes in rolling mode while visible. Day/range selections pause rolling;
Live restores the past 24 hours. Native reads and comparisons remain explicit.
No interactive Plotly widget or whole-array/raw-data fallback runs silently. Every new scientific view
provides its image, numerical product and metadata downloads where supported.
Saved investigations copy immutable plot pixels and preserve the selected data
and processing. Workspace queue refresh retains newly seen injection misses
locally; an explicit request changes `queued` to `requested`, not `running`.
There is no agent executor or Slack integration.

The interface uses a black background. Readiness leads with non-OK checks, then
disk capacity and observation/data-flow checks; detailed measurements are collapsed.
OVRO local (PDT/PST) is the default selection/plot clock; UTC is optional.
Hella DM plots use logarithmic spacing over 10–1000 pc cm^-3. The bottom
histogram shows linear counts per existing log-spaced bin. The width histogram
shows indices 0–6 with integer ticks. The 0–10 DM bucket and spare width bins
remain in stored/API evidence but are omitted from these figures. Recorded counts and
search configuration are unchanged. White figure surfaces match Visibilities;
the surrounding application stays dark. Search retains rolling 24-hour
coverage with five-minute refresh and separate zero/missing-coverage colours.

T1 emitted candidate distributions and raw-peak cap warnings have different
evidence. The fork's clustered output count cannot measure its pre-clustering
10k cap. Coverage and bounded log-tail limits must stay visible. Missing data,
missing artifacts and failed requests must not become zero counts or flat spectra.

Build staging shows the recipe and antenna selection. A separate confirmation
starts only the canonical Sun calibration driver under resource bounds; it is
not a deployment approval. **Get latest spectra** explicitly submits the existing
SNAP diagnostic read through a narrowly protected fixed loopback bridge. The
production worker retains serialization and cooldown; no parallel reader exists.
Acquisition times, overdue status and job state are distinct from display refresh.
Navigation performs no acquisition. Operational injection/dump, deployment and
restart-default changes remain disabled. Calibration comparison starts with a
verified reference product and matched local-clock windows, not two blank ranges.

## Implementation pointers

Source history has a five-button source bar: B0329 (default), Sun, Cyg A,
Cas A and Tau A. B0329 serves saved PDMP/filterbank plots. The other four use a
stationary beam at transit, ±2 hours, and show a dynamic spectrum with its
native-channel mean power curve below. Negative cross-power and missing data
are retained. Both plots open together in the zoom viewer. These are tests of
visibility phasing with the current calibration, not hardware-weight readback.
Completed windows are capped at three per source and cached in browser storage;
switching sources preserves figures and pending reads. Only the small catalogue
polls, every five minutes while selected. The calibration follows uploaded
weights through the deployment ledger, never a newly generated trial file.
The title/location scale up to 68/25 px on desktop. Overview, Visibilities and
SNAPs share larger green/blue antenna keys on white layout cards, with dark
navigation and compact controls. Operational SNAP safety details stay in docs;
failed/stale reads and missing history remain visible.
The unified tab row is text-only, with muted navy surfaces and a light selected
underline/border. Visibilities and SNAPs use a shallower header and wide,
compressed north-up maps, keeping the first plot row near the top. Less-used
visibility controls and detailed SNAP status are expandable. SNAP status chips
are white, with separate coloured Streaming/PPS labels and readable UTC times.
Keyboard focus, touchscreen target sizes and reduced-motion behavior remain.
Hourly spectrum reads now also run the getter-only cross-board PPS check under
the existing lease. The page only polls saved results. **PPS timing** compares
fresh advancing timestamps with explicitly accepted fixed offsets: unchanged is
green, changed/stalled is red, and failed/stale is Unknown. It no longer depends
on source-coherence evidence. Exact alignment remains separately available in
the API; a green accepted offset does not assert sample-exact alignment.
See [source-history API](../docs/api-commissioning.md#sun-cyg-a-cas-a-and-tau-a-visibility-history).

For real documentation screenshots and a bounded service CPU/RAM sample, run
`scripts/capture_monitor_docs.py --output docs/screenshots/YYYY-MM-DD` with the
offline Python environment from the repository root. It captures loaded live
views, never fixture responses or calibration/acquisition jobs. See
[resources and storage](../docs/resources-and-storage.md) for paths and limits.

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
