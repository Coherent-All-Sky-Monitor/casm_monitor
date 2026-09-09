// The design tokens, in one place (frontend/DESIGN.md). CSS gets them as
// custom properties in styles.css; Plotly needs them as JS strings, so they
// live here and the two copies are kept identical by hand.

export const PAPER = "#ffffff";
export const INK = "#1f2937";
export const MUTED = "#6b7280";
export const FAINT = "#9ca3af";
export const HAIRLINE = "#e5e7eb";
export const SIGNAL = "#2563eb";
export const ALERT = "#dc2626";
export const CAUTION = "#b45309";

export const SANS = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';

/** The sequential colourscale, used for amplitude, coherence and the autos
 * waterfalls: everywhere the house figures use viridis. */
export const COLORSCALE = "Viridis";

/** The diverging colourscale for phase and real/imag waterfalls, matching
 * matplotlib's RdBu_r in casm_vis_analysis (plotting/waterfall.py): negative
 * values red, positive blue, white at zero. Plotly's built-in "RdBu" runs
 * the other way (red high, blue low), so callers pass `reversescale: true`
 * alongside this to reproduce RdBu_r exactly. */
export const DIVERGING_COLORSCALE = "RdBu";
