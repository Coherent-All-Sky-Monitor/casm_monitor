# casm_monitor frontend design

1. The page is a matplotlib figure, not a dashboard: white paper, a grid of equal
   panels, thin lines, light grid, no boxes, badges, chips, cards or shadows.
2. Tokens, used exactly: paper #ffffff, ink #1f2937, muted #6b7280, faint #9ca3af,
   hairline #e5e7eb, signal #2563eb, alert #dc2626, caution #b45309; viridis only.
3. One typeface (system sans), 13 px body / 12 px panel titles / 18 px page title,
   tabular numerals, no monospace, no all-caps, no icons, no emoji, no gradients.
4. Spacing: 24 px page gutter, 16 px between panels, 40 px between sections; the
   panel grid fills the viewport, 6 columns at >= 1600 px, 4 at >= 1100, 3 below.
5. State is a sentence, not a colour-coded widget: the status line reads as prose
   and ends with the current UTC time; the full table is one click away, collapsed.
6. Problems are their own short sentence in alert colour. Nothing else is coloured.
7. Panel titles are `ant 26  N16E1  (pkt 25)` in muted 12 px, ink when the input is
   in the beamforming set; per-board facts live in one muted section line, not per panel.
8. Axis labels only where needed: ticks on the bottom row and left column of each
   board section; the only line colour is signal blue at 1 px, no legends, no modebar.
9. Every control is a plain word: segmented text with a hairline underline on the
   selection, one checkbox, one text button. No dropdowns for two-way choices.
10. Anything that is neither data nor a short sentence is removed.
