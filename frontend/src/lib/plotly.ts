// Single import point for Plotly. The cartesian bundle is the smallest
// prebuilt dist that carries the `heatmap` trace, which the SNAP waterfalls
// need (the `basic` bundle is scatter/bar/pie only, so a heatmap silently
// renders nothing). It is loaded lazily by lib/usePlotly.ts, so it stays in
// its own chunk. Do not import plotly.js anywhere else.
import Plotly from "plotly.js-cartesian-dist-min";

export default Plotly;
