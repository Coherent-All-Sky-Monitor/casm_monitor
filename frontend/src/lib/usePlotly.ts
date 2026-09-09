// Shared Plotly-mount hook for the SNAPs tab (and reusable from M2 onward):
// mounts into a div ref via a dynamic import of lib/plotly.ts (keeping
// Plotly in its own lazy chunk, per SmokeSparkline.tsx/docs/plan.md "keep
// the bundle lean"), then Plotly.react()s on data/layout changes so series
// update in place instead of a full remount, and purges on unmount.
//
// plotly.js-basic-dist-min ships no types (see
// lib/plotly-basic-dist-min.d.ts), so data/layout/config are typed `any`
// here rather than pretending to a shape we cannot check.
import { useEffect, useRef } from "react";

const DEFAULT_CONFIG = { displayModeBar: false, responsive: true };

export function usePlotly(data: any[], layout: any, config: any = DEFAULT_CONFIG) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let disposed = false;
    import("./plotly").then(({ default: Plotly }) => {
      if (disposed || !el) return;
      Plotly.react(el, data, layout, config);
    });
    return () => {
      disposed = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, layout, config]);

  useEffect(() => {
    const el = ref.current;
    return () => {
      if (el) {
        import("./plotly").then(({ default: Plotly }) => Plotly.purge(el));
      }
    };
  }, []);

  return ref;
}
