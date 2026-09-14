# Scientific operator workspace design

This supersedes the initial light, equal-panel, Plotly-oriented design.
The operator selects data, inspects scientific figures and preserves evidence.
The DSA `dsa110-rt/tools/dashboard/dsa_monitor` workflow is the reference for
useful task completion, including build, diagnostic review and separate approval.
It is not authority to copy deployment controls.

1. Dark neutral surfaces, readable axes and restrained blue controls. Current
   tokens live in `src/workspace.css`: paper `#10151c`, ink `#e2e8ef`, muted
   `#a0aebf`, hairline `#2b3644`, signal `#7cb8df`. No promotional cards,
   decorative emoji, gradients or purple UI accents.
2. Scientific figures use existing CASM Matplotlib routines. No Plotly runtime
   belongs in the reachable workspace. Scientific colormaps encode quantities,
   not decorative branding; phase and amplitude require distinct scales.
3. Give plots enough area to inspect. Geometry-based selection and compact
   time/frequency controls precede the figure; subordinate provenance and longer
   evidence remain accessible without dominating it.
4. Support today, another day and explicit intervals, with data availability and
   resource bounds visible. Re-rendering a chosen interval is the initial zoom
   interaction. Never invent data for unavailable ranges or silently switch
   readers/resolutions.
5. Every displayed scientific product has meaningful axes, units, processing,
   timestamp/coverage and a download link. Keep source acquisition, product
   rendering and browser-check times distinct. Missing samples remain gaps.
6. Labels distinguish wiring, intended participation and inspected product
   membership. Candidate occupancy does not prove searched-beam coverage;
   synthetic replay does not establish live injection recovery. Phase drift and
   rank-1 changes do not independently justify deployment.
7. Human actions have explicit states: save evidence, queue, request investigation;
   stage recipe, confirm build, review diagnostics. `Requested` is not `running`,
   and successful build is not deployment. No automatic investigator or Slack
   posting is connected. Every newly seen miss persists locally in workspace mode.
8. Keep focus indicators, readable labels and usable controls. Responsive stacking
   prevents horizontal overflow on phones; deeper phone-specific refinement is
   later work. Do not shrink plots into unreadable dashboard tiles.

The historical plan is retained in `../docs/plan.md`; its old colors and stack
choices must not override these requirements or the current API manuals.
