import { useEffect, useRef } from "react";

/**
 * M0 smoke test for the vendored Plotly bundle: draws a tiny random
 * sparkline. Nothing real is plotted until M1/M2; this only proves the
 * `lib/plotly.ts` import path works end to end in the production build.
 *
 * Plotly is loaded via a dynamic import so its ~450 kB (gzip) stays in its
 * own chunk, fetched only when this component actually mounts, instead of
 * bloating the main bundle every tab pays for (docs/plan.md "keep the
 * bundle lean").
 */
export default function SmokeSparkline() {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    let disposed = false;
    let plotted: HTMLDivElement | null = null;

    import("../lib/plotly").then(({ default: Plotly }) => {
      if (disposed || !ref.current) return;
      const n = 30;
      const y = Array.from({ length: n }, (_, i) => Math.sin(i / 3) + Math.random() * 0.2);
      const x = Array.from({ length: n }, (_, i) => i);

      Plotly.newPlot(
        ref.current,
        [{ x, y, type: "scatter", mode: "lines", line: { width: 1.5 } }],
        {
          margin: { l: 0, r: 0, t: 0, b: 0 },
          height: 40,
          width: 160,
          xaxis: { visible: false },
          yaxis: { visible: false },
          paper_bgcolor: "transparent",
          plot_bgcolor: "transparent",
        },
        { staticPlot: true, displayModeBar: false },
      );
      plotted = ref.current;
    });

    return () => {
      disposed = true;
      if (plotted) {
        import("../lib/plotly").then(({ default: Plotly }) => Plotly.purge(plotted!));
      }
    };
  }, []);

  return <div ref={ref} aria-label="plotly smoke test sparkline" />;
}
