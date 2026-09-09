// Shared Plotly styling so every chart in the app looks like one of the
// team's matplotlib figures: white paper, hairline grid, thin signal-blue
// line, no legend, no modebar, no title inside the plot.
//
// plotly.js ships no types in the dist bundles we use (lib/plotly.ts), so
// these builders return plain objects typed loosely on purpose.

import { FAINT, HAIRLINE, MUTED, PAPER, SANS, SIGNAL } from "./theme";

export const PLOT_CONFIG = { displayModeBar: false, responsive: true, staticPlot: false };

/** Axis defaults: a thin baseline, hairline grid, no zero line, muted ticks. */
export function axis(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    showline: true,
    linecolor: HAIRLINE,
    linewidth: 1,
    mirror: false,
    zeroline: false,
    showgrid: true,
    gridcolor: HAIRLINE,
    gridwidth: 1,
    ticks: "outside",
    ticklen: 3,
    tickcolor: HAIRLINE,
    tickfont: { size: 10, color: MUTED, family: SANS },
    title: { font: { size: 11, color: MUTED, family: SANS } },
    automargin: false,
    ...overrides,
  };
}

/** Layout defaults: white paper, no legend, minimal margins. */
export function layout(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    paper_bgcolor: PAPER,
    plot_bgcolor: PAPER,
    showlegend: false,
    font: { family: SANS, size: 11, color: MUTED },
    hoverlabel: { font: { family: SANS, size: 11 }, bgcolor: PAPER, bordercolor: HAIRLINE },
    ...overrides,
  };
}

/** The one line style: 1 px signal blue. */
export function line(
  x: (number | string)[],
  y: (number | null)[],
  color = SIGNAL,
): Record<string, unknown> {
  return {
    x,
    y,
    type: "scatter",
    mode: "lines",
    line: { width: 1, color },
    hoverinfo: "x+y",
  };
}

/** A light grey span across the full plot height, e.g. a dark subband or the
 * correlator passband inside the wider board-side band. */
export function span(x0: number, x1: number, fill: string): Record<string, unknown> {
  return {
    type: "rect",
    xref: "x",
    yref: "paper",
    x0,
    x1,
    y0: 0,
    y1: 1,
    fillcolor: fill,
    line: { width: 0 },
    layer: "below",
  };
}

/** Grey wash marking a subband the correlator is not delivering. */
export const DARK_SUBBAND_FILL = "rgba(156, 163, 175, 0.22)";
/** Even lighter wash marking the correlator band inside the board-side band. */
export const CORR_BAND_FILL = "rgba(37, 99, 235, 0.05)";

/** Thin vertical hairline (trend-plot epoch markers). */
export function vline(x: string | number, color = FAINT): Record<string, unknown> {
  return {
    type: "line",
    xref: "x",
    yref: "paper",
    x0: x,
    x1: x,
    y0: 0,
    y1: 1,
    line: { color, width: 1 },
    layer: "below",
  };
}
