import { useEffect, useMemo, useRef, useState } from "react";
import WaterfallCell from "./WaterfallCell";
import { getVisWaterfall } from "../../lib/api";
import { mockGetVisWaterfall } from "../../lib/mockVis";
import { waterfallLegendSentence, waterfallPanelTitle, waterfallPanelTooltip, waterfallWindowSentence } from "../../lib/visText";
import type { VisInputInfo, VisQuantity, VisRef, VisUnits, VisWaterfallResponse } from "../../lib/types";

const PANEL_SIZE = 110;
const MAX_CONCURRENT = 6;
const MAX_CELLS = 6000;

export interface WaterfallMatrixProps {
  /** Inputs in the selected set, in the order they should appear on both
   * axes. */
  inputs: VisInputInfo[];
  quantity: VisQuantity;
  units: VisUnits;
  reference: VisRef;
  t0: string;
  t1: string;
  useMock: boolean;
  onSelect: (i: number, j: number) => void;
}

interface CellSpec {
  row: number;
  col: number;
  i: number;
  j: number;
  isDiag: boolean;
}

function cacheKey(i: number, j: number, t0: string, t1: string, ref: VisRef, quantity: VisQuantity, units: VisUnits): string {
  return `${i}:${j}:${t0}:${t1}:${ref}:${quantity}:${units}`;
}

/**
 * The upper-triangle waterfall matrix for the crosses view: one small
 * heatmap panel per baseline (diagonal = autocorrelation amp/dB, off
 * diagonal = the selected quantity), fetched per baseline with limited
 * concurrency and cached by (i, j, window, ref, units, quantity) so toggling
 * back and forth between this and the spectra view never refetches. Panels
 * render as their fetch resolves rather than waiting for the whole matrix.
 */
export default function WaterfallMatrix({
  inputs,
  quantity,
  units,
  reference,
  t0,
  t1,
  useMock,
  onSelect,
}: WaterfallMatrixProps) {
  // Cache persists for the life of the mounted view; keyed so a toggle back
  // to a previously-seen (quantity, units, window) combination is instant.
  const cacheRef = useRef<Map<string, VisWaterfallResponse>>(new Map());
  const [, bump] = useState(0);
  const rerender = () => bump((x) => x + 1);

  const n = inputs.length;

  const cells: CellSpec[] = useMemo(() => {
    const list: CellSpec[] = [];
    for (let row = 0; row < n; row++) {
      for (let col = row; col < n; col++) {
        list.push({ row, col, i: inputs[row].packet_idx, j: inputs[col].packet_idx, isDiag: row === col });
      }
    }
    return list;
  }, [inputs, n]);

  useEffect(() => {
    let cancelled = false;
    const cache = cacheRef.current;
    const pending = cells.filter((c) => {
      const q = c.isDiag ? "amp" : quantity;
      const u = c.isDiag ? "db" : units;
      return !cache.has(cacheKey(c.i, c.j, t0, t1, reference, q, u));
    });
    let idx = 0;
    async function worker() {
      while (!cancelled) {
        const c = pending[idx++];
        if (!c) return;
        const q: VisQuantity = c.isDiag ? "amp" : quantity;
        const u: VisUnits = c.isDiag ? "db" : units;
        const fetcher = useMock
          ? () => mockGetVisWaterfall(c.i, c.j, t0, t1, q, u, reference)
          : () => getVisWaterfall({ i: c.i, j: c.j, t0, t1, quantity: q, units: u, ref: reference, max_cells: MAX_CELLS });
        try {
          const r = await fetcher();
          if (cancelled) return;
          cache.set(cacheKey(c.i, c.j, t0, t1, reference, q, u), r);
          rerender();
        } catch {
          // lib/api.ts already toasts the error; the panel just stays empty.
        }
      }
    }
    const workers = Array.from({ length: Math.min(MAX_CONCURRENT, pending.length) }, () => worker());
    Promise.all(workers);
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cells, quantity, units, reference, t0, t1, useMock]);

  if (n === 0) {
    return <p className="note">No inputs in this set yet.</p>;
  }

  return (
    <div>
      <p className="note" style={{ marginBottom: "var(--panel-gap)" }}>
        {waterfallWindowSentence(t0, t1, reference)}
      </p>
      <div className="waterfall-matrix-wrap">
        <div
          className="waterfall-matrix"
          style={{ gridTemplateColumns: `repeat(${n}, ${PANEL_SIZE}px)`, gridTemplateRows: `repeat(${n}, auto)` }}
        >
          {cells.map((c) => {
            const q: VisQuantity = c.isDiag ? "amp" : quantity;
            const u: VisUnits = c.isDiag ? "db" : units;
            const data = cacheRef.current.get(cacheKey(c.i, c.j, t0, t1, reference, q, u)) ?? null;
            const inputI = inputs[c.row];
            const inputJ = c.isDiag ? null : inputs[c.col];
            return (
              <div key={`${c.i}-${c.j}`} style={{ gridColumn: c.col + 1, gridRow: c.row + 1 }}>
                <WaterfallCell
                  title={waterfallPanelTitle(inputI, inputJ)}
                  tooltip={waterfallPanelTooltip(inputI, inputJ)}
                  data={data}
                  quantity={q}
                  units={u}
                  isDiagonal={c.isDiag}
                  size={PANEL_SIZE}
                  showXTicks={c.isDiag}
                  showYTicks={c.isDiag}
                  onClick={() => onSelect(c.i, c.j)}
                />
              </div>
            );
          })}
        </div>
        <p className="legend-text">{waterfallLegendSentence(quantity, units)}</p>
      </div>
    </div>
  );
}
