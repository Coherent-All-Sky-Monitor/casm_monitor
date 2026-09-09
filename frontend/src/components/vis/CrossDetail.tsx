import { useEffect, useMemo, useState } from "react";
import TimeRangePicker from "../TimeRangePicker";
import { usePlotly } from "../../lib/usePlotly";
import { useUrlParam } from "../../lib/useUrlParam";
import { resolveSince } from "../../lib/timeRange";
import { PLOT_CONFIG, axis, layout, line } from "../../lib/plotStyle";
import { CORR_BAND_MHZ } from "../../lib/snapConstants";
import { cellColor } from "../../lib/colorScale";
import { MUTED, SANS } from "../../lib/theme";
import { getVisSpectra, getVisWaterfall } from "../../lib/api";
import { mockGetVisSpectra, mockGetVisWaterfall } from "../../lib/mockVis";
import type { VisPairs, VisQuantity, VisRef, VisSet, VisUnits, VisWaterfallResponse } from "../../lib/types";

const SAWTOOTH_NOTE =
  "A sawtooth here (phase wrapping linearly with frequency) means an uncorrected delay on this baseline.";

const FRINGE_NOTE =
  "A linear phase slope across frequency is an uncorrected delay; horizontal stripes drifting in time are fringes.";

export interface CrossDetailProps {
  i: number;
  j: number;
  antI: number | null;
  antJ: number | null;
  set: VisSet;
  quantity: VisQuantity;
  units: VisUnits;
  reference: VisRef;
  useMock: boolean;
  onBack: () => void;
}

/** The expanded view for one baseline: the full-resolution spectrum, a
 * waterfall over the chosen window (viridis, descending frequency axis), and
 * — when the current quantity is phase — a note on reading the sawtooth as
 * an uncorrected delay. Replaces the crosses grid rather than floating over
 * it, matching the SNAPs InputDetail pattern. */
export default function CrossDetail({ i, j, antI, antJ, set, quantity, units, reference, useMock, onBack }: CrossDetailProps) {
  const isAuto = i === j;
  // A diagonal panel is always the autocorrelation amp/dB (the matrix's own
  // convention for the diagonal), regardless of the quantity/units the rest
  // of the crosses view is showing; `coh` is also undefined on an autoco.
  const effQuantity: VisQuantity = isAuto ? "amp" : quantity;
  const effUnits: VisUnits = isAuto ? "db" : units;
  const pairs: VisPairs = isAuto ? "auto" : "cross";

  const [range] = useUrlParam("cross_range_range", "24h");
  const [customFrom] = useUrlParam("cross_range_from", "");
  const [spectrumY, setSpectrumY] = useState<(number | null)[] | null>(null);
  const [freqMhz, setFreqMhz] = useState<number[]>([]);
  const [waterfall, setWaterfall] = useState<VisWaterfallResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    const p = useMock
      ? mockGetVisSpectra("latest", set, pairs, effQuantity, effUnits, reference)
      : getVisSpectra({ ts: "latest", set, pairs, quantity: effQuantity, units: effUnits, ref: reference });
    p.then((r) => {
      if (cancelled) return;
      setFreqMhz(r.freq_mhz);
      const bl = r.baselines.find((b) => b.i === i && b.j === j);
      setSpectrumY(bl?.y ?? null);
    }).catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [i, j, set, pairs, effQuantity, effUnits, reference, useMock]);

  useEffect(() => {
    let cancelled = false;
    const t1 = new Date().toISOString();
    const t0 = resolveSince(range, customFrom) || new Date(Date.now() - 24 * 3600_000).toISOString();
    const p = useMock
      ? mockGetVisWaterfall(i, j, t0, t1, effQuantity, effUnits, reference)
      : getVisWaterfall({ i, j, t0, t1, quantity: effQuantity, units: effUnits, ref: reference });
    p.then((r) => !cancelled && setWaterfall(r)).catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [i, j, range, customFrom, effQuantity, effUnits, reference, useMock]);

  const spectrumData = useMemo(() => (spectrumY ? [line(freqMhz, spectrumY)] : []), [freqMhz, spectrumY]);
  const spectrumLayout = useMemo(
    () =>
      layout({
        height: 300,
        margin: { l: 56, r: 8, t: 4, b: 40 },
        xaxis: axis({
          range: [CORR_BAND_MHZ[1], CORR_BAND_MHZ[0]],
          title: { text: "freq [MHz]", font: { size: 11, color: MUTED, family: SANS } },
        }),
        yaxis: axis({ title: { text: effUnits, font: { size: 11, color: MUTED, family: SANS } } }),
      }),
    [effUnits],
  );
  const spectrumRef = usePlotly(spectrumData, spectrumLayout, PLOT_CONFIG);

  const waterfallData = useMemo(() => {
    if (!waterfall) return [];
    const color = cellColor(effQuantity, effUnits, waterfall.z, isAuto);
    return [
      {
        x: waterfall.freq_mhz,
        y: waterfall.t.map((s) => new Date(s * 1000).toISOString()),
        z: waterfall.z,
        type: "heatmap",
        zsmooth: false,
        colorscale: color.colorscale,
        reversescale: color.reversescale,
        ...(color.zmin !== undefined ? { zmin: color.zmin, zmax: color.zmax } : {}),
        colorbar: {
          title: { text: effUnits, font: { size: 11, color: MUTED, family: SANS } },
          thickness: 10,
          outlinewidth: 0,
          tickfont: { size: 10, color: MUTED, family: SANS },
        },
        hoverinfo: "x+y+z",
      },
    ];
  }, [waterfall, effQuantity, effUnits, isAuto]);
  const waterfallLayout = useMemo(
    () =>
      layout({
        height: 380,
        margin: { l: 90, r: 8, t: 4, b: 40 },
        xaxis: axis({
          range: [CORR_BAND_MHZ[1], CORR_BAND_MHZ[0]],
          showgrid: false,
          title: { text: "freq [MHz]", font: { size: 11, color: MUTED, family: SANS } },
        }),
        yaxis: axis({ showgrid: false }),
      }),
    [],
  );
  const waterfallPlotRef = usePlotly(waterfallData, waterfallLayout, PLOT_CONFIG);

  const iLabel = antI !== null ? `ant ${antI}` : `pkt ${i}`;
  const jLabel = antJ !== null ? `ant ${antJ}` : `pkt ${j}`;

  return (
    <div>
      <button className="detail__back" onClick={onBack} type="button">
        back to the cross grid
      </button>
      <h2 className="detail__title">{isAuto ? iLabel : `${iLabel} x ${jLabel}`}</h2>

      <div className="detail__block" ref={spectrumRef} />
      {effQuantity === "phase" && <p className="detail__caption">{SAWTOOTH_NOTE}</p>}

      <div className="detail__block">
        <div className="toolbar">
          <TimeRangePicker paramPrefix="cross_range" defaultRange="24h" />
        </div>
        {waterfall ? <div ref={waterfallPlotRef} /> : <p className="note">No history for this baseline yet.</p>}
        {waterfall && effQuantity === "phase" && <p className="detail__caption">{FRINGE_NOTE}</p>}
      </div>
    </div>
  );
}
