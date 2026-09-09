import { useMemo } from "react";
import { usePlotly } from "../../lib/usePlotly";
import { downsample } from "../../lib/spectrumUtils";
import { PLOT_CONFIG, axis, layout, line } from "../../lib/plotStyle";
import { CORR_BAND_MHZ } from "../../lib/snapConstants";

const PANEL_MAX_POINTS = 320;
const PLOT_HEIGHT = 150;

export interface VisSpectrumPanelProps {
  /** `ant 26  N16E1  (pkt 25)`, already assembled by the caller. */
  title: string;
  inBf: boolean;
  freqMhz: number[];
  values: (number | null)[] | null;
  showXTicks: boolean;
  showYTicks: boolean;
}

/**
 * One autocorrelation panel: a muted title and a single 1 px signal-blue
 * line on white paper with a hairline grid, matching the SNAPs card exactly
 * (components/snaps/SpectrumPanel.tsx) but over the Visibilities correlator
 * band rather than a board's ADC channels.
 */
export default function VisSpectrumPanel({
  title,
  inBf,
  freqMhz,
  values,
  showXTicks,
  showYTicks,
}: VisSpectrumPanelProps) {
  const hasData = values !== null && values.some((v) => v !== null && !Number.isNaN(v));

  const data = useMemo(() => {
    if (!hasData) return [];
    const ds = downsample(freqMhz, values ?? [], PANEL_MAX_POINTS);
    return [line(ds.freq, ds.values)];
  }, [hasData, freqMhz, values]);

  const plotLayout = useMemo(
    () =>
      layout({
        height: PLOT_HEIGHT,
        margin: { l: showYTicks ? 34 : 6, r: 4, t: 2, b: showXTicks ? 22 : 6 },
        xaxis: axis({
          range: [CORR_BAND_MHZ[1], CORR_BAND_MHZ[0]], // descending, house-figure convention
          showticklabels: showXTicks,
        }),
        yaxis: axis({ showticklabels: showYTicks }),
      }),
    [showXTicks, showYTicks],
  );

  const ref = usePlotly(data, plotLayout, PLOT_CONFIG);

  return (
    <div className="panel">
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
    </div>
  );
}
