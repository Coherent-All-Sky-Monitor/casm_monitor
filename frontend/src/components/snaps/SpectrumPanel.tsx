import { useMemo } from "react";
import { usePlotly } from "../../lib/usePlotly";
import { convertSeries, downsample } from "../../lib/spectrumUtils";
import {
  CORR_BAND_FILL,
  DARK_SUBBAND_FILL,
  PLOT_CONFIG,
  axis,
  layout,
  line,
  span,
} from "../../lib/plotStyle";
import { BOARD_BAND_MHZ, CORR_BAND_MHZ, SUBBAND_EDGES_MHZ } from "../../lib/snapConstants";

const PANEL_MAX_POINTS = 320;
const PLOT_HEIGHT = 150;

export interface SpectrumPanelProps {
  /** `ant 26  N16E1  (pkt 25)`, already assembled by the caller. */
  title: string;
  /** Included in the live beamforming set: ink rather than muted. */
  inBf: boolean;
  freqMhz: number[];
  values: (number | null)[] | null;
  units: "dB" | "linear";
  layer: "correlator" | "board";
  /** Subband indices (0..5) the correlator is not delivering; drawn as a
   * grey span across the panel. */
  darkSubbands: number[];
  showXTicks: boolean;
  showYTicks: boolean;
  onClick: () => void;
}

/**
 * One input's spectrum: a muted title and a single 1 px signal-blue line on
 * white paper with a hairline grid. No legend, no modebar, no title inside
 * the plot; tick labels only where the caller says so (bottom row and left
 * column of each board section).
 */
export default function SpectrumPanel({
  title,
  inBf,
  freqMhz,
  values,
  units,
  layer,
  darkSubbands,
  showXTicks,
  showYTicks,
  onClick,
}: SpectrumPanelProps) {
  const hasData = values !== null && values.some((v) => v !== null && !Number.isNaN(v));

  const data = useMemo(() => {
    if (!hasData) return [];
    const ds = downsample(freqMhz, values ?? [], PANEL_MAX_POINTS);
    return [line(ds.freq, convertSeries(ds.values, units))];
  }, [hasData, freqMhz, values, units]);

  const plotLayout = useMemo(() => {
    const band = layer === "board" ? BOARD_BAND_MHZ : CORR_BAND_MHZ;
    const shapes = darkSubbands.map((sb) =>
      span(SUBBAND_EDGES_MHZ[sb][0], SUBBAND_EDGES_MHZ[sb][1], DARK_SUBBAND_FILL),
    );
    if (layer === "board") {
      shapes.push(span(CORR_BAND_MHZ[0], CORR_BAND_MHZ[1], CORR_BAND_FILL));
    }
    return layout({
      height: PLOT_HEIGHT,
      margin: { l: showYTicks ? 34 : 6, r: 4, t: 2, b: showXTicks ? 22 : 6 },
      xaxis: axis({
        range: [band[1], band[0]], // descending, as in the house figures
        showticklabels: showXTicks,
      }),
      yaxis: axis({ showticklabels: showYTicks }),
      shapes,
    });
  }, [layer, darkSubbands, showXTicks, showYTicks]);

  const ref = usePlotly(data, plotLayout, PLOT_CONFIG);

  return (
    <button className="panel" onClick={onClick} type="button">
      <div className={`panel__title${inBf ? " panel__title--bf" : ""}`} title={title}>
        {title}
      </div>
      {hasData ? (
        <div ref={ref} />
      ) : (
        <div className="panel__empty" style={{ height: PLOT_HEIGHT }}>
          no data
        </div>
      )}
    </button>
  );
}
