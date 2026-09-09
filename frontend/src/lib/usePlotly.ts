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
  // The element Plotly was last drawn into. Kept separately from `ref` so
  // the unmount cleanup can purge a div that only mounted later (the
  // waterfall and trend divs render once their data arrives), and so it does
  // not depend on when React detaches the ref.
  const drawnEl = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let disposed = false;
    import("./plotly").then(({ default: Plotly }) => {
      if (disposed) return;
      drawnEl.current = el;
      Plotly.react(el, data, layout, config);
    });
    return () => {
      disposed = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, layout, config]);

  useEffect(() => {
    return () => {
      const el = drawnEl.current;
      if (el) {
        drawnEl.current = null;
        import("./plotly").then(({ default: Plotly }) => Plotly.purge(el));
      }
    };
  }, []);

  return ref;
}
