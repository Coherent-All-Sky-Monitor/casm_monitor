import { useEffect, useMemo, useState } from "react";
import MatrixHeatmap from "./MatrixHeatmap";
import { lastNightWindowPT } from "../../lib/visText";
import { getVisCoherence } from "../../lib/api";
import { mockGetVisCoherence } from "../../lib/mockVis";
import type { VisInputInfo, VisSet } from "../../lib/types";

export interface CoherenceViewProps {
  set: VisSet;
  inputsByPacket: Map<number, VisInputInfo>;
  useMock: boolean;
}

/** The night coherence matrix over a fixed default window (last night
 * 02:00-05:00 PT), stated as a sentence rather than a picker. */
export default function CoherenceView({ set, inputsByPacket, useMock }: CoherenceViewProps) {
  const window = useMemo(() => lastNightWindowPT(), []);
  const [result, setResult] = useState<{ inputs: number[]; m: number[][] } | null>(null);

  useEffect(() => {
    let cancelled = false;
    const fetcher = useMock ? mockGetVisCoherence : getVisCoherence;
    fetcher(window.t0, window.t1, set)
      .then((r) => !cancelled && setResult(r))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [window, set, useMock]);

  return (
    <div>
      <p className="note" style={{ marginBottom: "var(--panel-gap)" }}>
        {window.sentence}
      </p>
      {result && result.inputs.length > 0 ? (
        <MatrixHeatmap
          packetIdxs={result.inputs}
          m={result.m}
          inputsByPacket={inputsByPacket}
          colorbarTitle="coherence"
        />
      ) : (
        <p className="note">No coherence data for this window yet.</p>
      )}
    </div>
  );
}
