// Single import point for Plotly. plotly.js-basic-dist-min bundles only the
// scatter/bar traces this app needs (spectra, matrices as heatmaps, images)
// so the vendored bundle stays lean; do not import plotly.js elsewhere.
// M1/M2 code should `import Plotly from "../lib/plotly"`.
import Plotly from "plotly.js-basic-dist-min";

export default Plotly;
