import VisSpectrumPanel from "./VisSpectrumPanel";
import { isBottomRow, isLeftColumn, useColumns } from "../../lib/useColumns";
import { visPanelTitle } from "../../lib/visText";
import type { VisInputInfo, VisSpectraResponse } from "../../lib/types";

export interface AutosGridProps {
  inputs: VisInputInfo[];
  spectra: VisSpectraResponse | null;
}

/** One panel per wired/live input, its autocorrelation, in the same equal
 * panel grid as the SNAPs board sections. */
export default function AutosGrid({ inputs, spectra }: AutosGridProps) {
  const cols = useColumns();
  const byPacket = new Map((spectra?.baselines ?? []).map((b) => [b.i, b]));
  if (inputs.length === 0) {
    return <p className="note">No inputs in this set yet.</p>;
  }
  return (
    <div className="grid">
      {inputs.map((input, i) => {
        const bl = byPacket.get(input.packet_idx);
        return (
          <VisSpectrumPanel
            key={input.packet_idx}
            title={visPanelTitle(input)}
            inBf={input.in_bf}
            freqMhz={spectra?.freq_mhz ?? []}
            values={bl?.y ?? null}
            showXTicks={isBottomRow(i, inputs.length, cols)}
            showYTicks={isLeftColumn(i, cols)}
          />
        );
      })}
    </div>
  );
}
