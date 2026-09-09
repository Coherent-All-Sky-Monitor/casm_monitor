import { useMemo } from "react";
import { usePlotly } from "../../lib/usePlotly";
import { PLOT_CONFIG, axis, layout } from "../../lib/plotStyle";
import { COLORSCALE, MUTED, SANS } from "../../lib/theme";
import type { VisInputInfo } from "../../lib/types";

export interface MatrixHeatmapProps {
  /** Packet indices for each row/column, same order both axes. */
  packetIdxs: number[];
  m: number[][];
  inputsByPacket: Map<number, VisInputInfo>;
  colorbarTitle: string;
}

/** An NxN viridis heatmap with antenna numbers on both axes, shared by the
 * Visibilities `matrix` and `coherence` views. */
export default function MatrixHeatmap({ packetIdxs, m, inputsByPacket, colorbarTitle }: MatrixHeatmapProps) {
  const labels = useMemo(
    () => packetIdxs.map((p) => String(inputsByPacket.get(p)?.antenna ?? `p${p}`)),
    [packetIdxs, inputsByPacket],
  );

  const data = useMemo(
    () => [
      {
        x: labels,
        y: labels,
        z: m,
        type: "heatmap",
        colorscale: COLORSCALE,
        colorbar: {
          title: { text: colorbarTitle, font: { size: 11, color: MUTED, family: SANS } },
          thickness: 10,
          outlinewidth: 0,
          tickfont: { size: 10, color: MUTED, family: SANS },
        },
        hoverinfo: "x+y+z",
      },
    ],
    [labels, m, colorbarTitle],
  );

  const plotLayout = useMemo(
    () =>
      layout({
        height: Math.max(420, Math.min(900, labels.length * 22)),
        margin: { l: 48, r: 8, t: 8, b: 48 },
        xaxis: axis({ type: "category", title: { text: "antenna", font: { size: 11, color: MUTED, family: SANS } } }),
        yaxis: axis({
          type: "category",
          autorange: "reversed",
          title: { text: "antenna", font: { size: 11, color: MUTED, family: SANS } },
        }),
      }),
    [labels.length],
  );

  const ref = usePlotly(data, plotLayout, PLOT_CONFIG);
  return <div ref={ref} />;
}
