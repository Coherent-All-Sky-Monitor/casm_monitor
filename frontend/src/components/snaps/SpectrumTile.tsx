import { usePlotly } from "../../lib/usePlotly";
import { convertSeries, downsample } from "../../lib/spectrumUtils";

const TILE_MAX_POINTS = 256;

export interface SpectrumTileProps {
  title: string;
  freqMhz: number[];
  values: (number | null)[] | null;
  units: "dB" | "linear";
  /** Correlator passband to shade when this tile is showing a board-side
   * (wider) spectrum, e.g. [390.6, 484.4]. */
  shadeBand?: [number, number];
  bold?: boolean;
  dimmed?: boolean;
  /** mapping === "mismatch": a validation pass disagrees with the formula
   * row assignment for this input -- an operator-visible red flag, not a
   * rejection (the tile still renders the formula row's data). */
  mismatch?: boolean;
  height?: number;
  onClick?: () => void;
}

/** One small spectrum plot in the 3x4 card grid (or the wider board-read
 * tile). Downsamples client-side so a full-res 3072/4096-ch response does
 * not push that many points into a ~120px-tall chart. */
export default function SpectrumTile({
  title,
  freqMhz,
  values,
  units,
  shadeBand,
  bold,
  dimmed,
  mismatch,
  height = 120,
  onClick,
}: SpectrumTileProps) {
  const hasData = values !== null && values.some((v) => v !== null && !Number.isNaN(v));
  const { freq: dsFreq, values: dsValues } = downsample(freqMhz, values ?? [], TILE_MAX_POINTS);
  const y = convertSeries(dsValues, units);

  const data = [
    {
      x: dsFreq,
      y,
      type: "scatter",
      mode: "lines",
      line: { width: 1, color: hasData ? (dimmed ? "#9aa2b1" : "#2258d6") : "#c94040" },
      hoverinfo: "x+y",
    },
  ];
  const shapes = shadeBand
    ? [
        {
          type: "rect",
          xref: "x",
          yref: "paper",
          x0: shadeBand[0],
          x1: shadeBand[1],
          y0: 0,
          y1: 1,
          fillcolor: "rgba(34,88,214,0.08)",
          line: { width: 0 },
        },
      ]
    : [];
  const layout = {
    margin: { l: 30, r: 4, t: 4, b: 16 },
    height,
    xaxis: { tickfont: { size: 8 }, showgrid: false },
    yaxis: {
      title: units === "dB" ? "dB" : "lin",
      tickfont: { size: 8 },
      titlefont: { size: 8 },
      showgrid: true,
      gridcolor: "rgba(128,128,128,0.15)",
    },
    shapes,
    paper_bgcolor: "transparent",
    plot_bgcolor: "transparent",
    font: { size: 8 },
  };
  const ref = usePlotly(data, layout);

  return (
    <div
      className={`snap-tile${bold ? " snap-tile--bold" : ""}${dimmed ? " snap-tile--dim" : ""}`}
      onClick={onClick}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
    >
      <div className="snap-tile__title" title={title}>
        {title}
      </div>
      <div ref={ref} />
      {!hasData && <div className="snap-tile__nodata">no data</div>}
      {mismatch && (
        <span className="snap-tile__badge--mismatch" title="row mapping validation disagrees with the formula">
          mismatch
        </span>
      )}
    </div>
  );
}
