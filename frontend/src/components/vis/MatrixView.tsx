import { useEffect, useMemo, useState } from "react";
import MatrixHeatmap from "./MatrixHeatmap";
import { useUrlParam } from "../../lib/useUrlParam";
import { CORR_BAND_MHZ } from "../../lib/snapConstants";
import { getVisMatrix } from "../../lib/api";
import { mockGetVisMatrix } from "../../lib/mockVis";
import type { VisInputInfo, VisQuantity, VisSet, VisUnits } from "../../lib/types";

export interface MatrixViewProps {
  ts: "latest" | number;
  set: VisSet;
  quantity: VisQuantity;
  units: VisUnits;
  inputsByPacket: Map<number, VisInputInfo>;
  useMock: boolean;
}

/** The NxN amplitude/phase/etc. matrix at one integration, with a band
 * range picker (two numeric inputs, default the whole correlator band). */
export default function MatrixView({ ts, set, quantity, units, inputsByPacket, useMock }: MatrixViewProps) {
  const [fminStr, setFminStr] = useUrlParam("vis_fmin", String(CORR_BAND_MHZ[0]));
  const [fmaxStr, setFmaxStr] = useUrlParam("vis_fmax", String(CORR_BAND_MHZ[1]));
  const [result, setResult] = useState<{ inputs: number[]; m: number[][] } | null>(null);

  const fmin = Number(fminStr) || CORR_BAND_MHZ[0];
  const fmax = Number(fmaxStr) || CORR_BAND_MHZ[1];

  useEffect(() => {
    let cancelled = false;
    const fetcher = useMock ? mockGetVisMatrix : (
      (t: "latest" | number, s: VisSet, q: VisQuantity, u: VisUnits, lo?: number, hi?: number) =>
        getVisMatrix({ ts: t, set: s, quantity: q, units: u, fmin: lo, fmax: hi })
    );
    fetcher(ts, set, quantity, units, fmin, fmax)
      .then((r) => !cancelled && setResult(r))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [ts, set, quantity, units, fmin, fmax, useMock]);

  const colorbarTitle = useMemo(() => (quantity === "phase" ? units : units.toUpperCase()), [quantity, units]);

  return (
    <div>
      <div className="band-range">
        <span>band</span>
        <input
          type="number"
          value={fminStr}
          onChange={(e) => setFminStr(e.target.value)}
          aria-label="band fmin MHz"
        />
        <span>-</span>
        <input
          type="number"
          value={fmaxStr}
          onChange={(e) => setFmaxStr(e.target.value)}
          aria-label="band fmax MHz"
        />
        <span>MHz</span>
      </div>
      {result && result.inputs.length > 0 ? (
        <MatrixHeatmap
          packetIdxs={result.inputs}
          m={result.m}
          inputsByPacket={inputsByPacket}
          colorbarTitle={colorbarTitle}
        />
      ) : (
        <p className="note">No matrix for this set yet.</p>
      )}
    </div>
  );
}
