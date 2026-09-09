// The plotly.js dist bundles ship no types. Fall back to `any` so callers
// still get module resolution; tighten with @types/plotly.js if it becomes
// available through the proxy in a later milestone.
declare module "plotly.js-cartesian-dist-min" {
  const Plotly: any;
  export default Plotly;
}
