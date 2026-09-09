import { useMemo } from "react";
import { usePlotly } from "../../lib/usePlotly";
import { PLOT_CONFIG, axis, layout } from "../../lib/plotStyle";
import { cellColor, transposeZ } from "../../lib/colorScale";
import { MUTED, SANS } from "../../lib/theme";
import type { VisQuantity, VisUnits, VisWaterfallResponse } from "../../lib/types";

export interface WaterfallCellProps {
  /** `ant 26` or `ant 26 x ant 30`, shown as the panel title. */
  title: string;
  /** The full `ant N  station  (pkt idx)` pair, as a native tooltip. */
  tooltip: string;
  data: VisWaterfallResponse | null;
  quantity: VisQuantity;
  units: VisUnits;
  isDiagonal: boolean;
  size: number;
  /** Ticks only on the outer left column (freq) and the outer bottom row
   * (time) of the matrix, per DESIGN.md rule 8; for the triangular
   * waterfall matrix that is exactly the diagonal cell of each row/column. */
  showXTicks: boolean;
  showYTicks: boolean;
  onClick: () => void;
}

/**
 * One small waterfall panel in the Visibilities crosses matrix: time on x
 * (hours from the window start), frequency on y (MHz, descending), no
 * colorbar and no hover (the click target opens the full expanded view).
 */
export default function WaterfallCell({
  title,
  tooltip,
  data,
  quantity,
  units,
  isDiagonal,
  size,
  showXTicks,
  showYTicks,
  onClick,
}: WaterfallCellProps) {
  const plotData = useMemo(() => {
    if (!data || data.t.length === 0 || data.freq_mhz.length === 0) return [];
    const hours = data.t.map((s) => (s - data.t[0]) / 3600);
    const color = cellColor(quantity, units, data.z, isDiagonal);
    return [
      {
        x: hours,
        y: data.freq_mhz,
        z: transposeZ(data.z),
        type: "heatmap",
        zsmooth: false,
        colorscale: color.colorscale,
        reversescale: color.reversescale,
        ...(color.zmin !== undefined ? { zmin: color.zmin, zmax: color.zmax } : {}),
        showscale: false,
        hoverinfo: "skip",
      },
    ];
  }, [data, quantity, units, isDiagonal]);

  const plotLayout = useMemo(
    () =>
      layout({
        height: size,
        width: size,
        margin: {
          l: showYTicks ? 30 : 2,
          r: 2,
          t: 2,
          b: showXTicks ? 16 : 2,
        },
        xaxis: axis({
          showgrid: false,
          showticklabels: showXTicks,
          title: showXTicks ? { text: "h", font: { size: 9, color: MUTED, family: SANS } } : undefined,
          nticks: 3,
        }),
        yaxis: axis({
          showgrid: false,
          showticklabels: showYTicks,
          autorange: "reversed",
          nticks: 3,
        }),
      }),
    [size, showXTicks, showYTicks],
  );

  const ref = usePlotly(plotData, plotLayout, PLOT_CONFIG);

  return (
    <button className="waterfall-cell" type="button" title={tooltip} onClick={onClick}>
      <div className="waterfall-cell__title">{title}</div>
      {plotData.length > 0 ? (
        <div ref={ref} style={{ width: size, height: size }} />
      ) : (
        <div className="waterfall-cell__empty" style={{ width: size, height: size }} />
      )}
    </button>
  );
}
