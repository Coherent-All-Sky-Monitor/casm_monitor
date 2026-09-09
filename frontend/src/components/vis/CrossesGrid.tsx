import { useMemo } from "react";
import { usePlotly } from "../../lib/usePlotly";
import { PLOT_CONFIG, axis, layout, line } from "../../lib/plotStyle";
import { CORR_BAND_MHZ } from "../../lib/snapConstants";
import { crossAxisLabel } from "../../lib/visText";
import type { VisBaseline, VisInputInfo, VisSpectraResponse } from "../../lib/types";

const CELL_HEIGHT = 44;

function CrossCell({
  freqMhz,
  values,
  onClick,
}: {
  freqMhz: number[];
  values: (number | null)[] | null;
  onClick: () => void;
}) {
  const hasData = values !== null && values.some((v) => v !== null && !Number.isNaN(v));
  const data = useMemo(() => (hasData ? [line(freqMhz, values ?? [])] : []), [hasData, freqMhz, values]);
  const plotLayout = useMemo(
    () =>
      layout({
        height: CELL_HEIGHT,
        margin: { l: 2, r: 2, t: 2, b: 2 },
        xaxis: axis({ range: [CORR_BAND_MHZ[1], CORR_BAND_MHZ[0]], showticklabels: false, showgrid: false }),
        yaxis: axis({ showticklabels: false, showgrid: false }),
      }),
    [],
  );
  const ref = usePlotly(data, plotLayout, PLOT_CONFIG);
  return (
    <button className="cross-cell" onClick={onClick} type="button">
      {hasData ? <div ref={ref} /> : <div className="cross-cell__empty" style={{ height: CELL_HEIGHT }} />}
    </button>
  );
}

export interface CrossesGridProps {
  inputs: VisInputInfo[];
  spectra: VisSpectraResponse | null;
  onSelect: (i: number, j: number) => void;
}

/** The upper triangle of the cross-correlation matrix (row i, column j, only
 * j > i), one small single-line panel per baseline, rows and columns
 * labelled by antenna number in muted text. */
export default function CrossesGrid({ inputs, spectra, onSelect }: CrossesGridProps) {
  const byPair = useMemo(() => {
    const m = new Map<string, VisBaseline>();
    for (const b of spectra?.baselines ?? []) m.set(`${b.i}:${b.j}`, b);
    return m;
  }, [spectra]);

  if (inputs.length < 2) {
    return <p className="note">Fewer than two inputs in this set; no baselines to show.</p>;
  }

  const n = inputs.length;
  const cells = [<div key="corner" />];
  for (const col of inputs) {
    cells.push(
      <div key={`h-${col.packet_idx}`} className="cross-grid__label">
        {crossAxisLabel(col)}
      </div>,
    );
  }
  for (let i = 0; i < n; i++) {
    cells.push(
      <div key={`r-${inputs[i].packet_idx}`} className="cross-grid__label">
        {crossAxisLabel(inputs[i])}
      </div>,
    );
    for (let j = 0; j < n; j++) {
      if (j <= i) {
        cells.push(<div key={`e-${i}-${j}`} />);
        continue;
      }
      const bl = byPair.get(`${inputs[i].packet_idx}:${inputs[j].packet_idx}`);
      cells.push(
        <CrossCell
          key={`c-${i}-${j}`}
          freqMhz={spectra?.freq_mhz ?? []}
          values={bl?.y ?? null}
          onClick={() => onSelect(inputs[i].packet_idx, inputs[j].packet_idx)}
        />,
      );
    }
  }

  return (
    <div className="cross-grid-wrap">
      <div className="cross-grid" style={{ gridTemplateColumns: `40px repeat(${n}, minmax(46px, 1fr))` }}>
        {cells}
      </div>
    </div>
  );
}
