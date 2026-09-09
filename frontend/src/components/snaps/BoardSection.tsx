import SpectrumPanel from "./SpectrumPanel";
import { isBottomRow, isLeftColumn, useColumns } from "../../lib/useColumns";
import { boardLine, boardProblems, darkSubbands, panelTitle } from "../../lib/snapText";
import { CORR_AGE_STALE_S } from "../../lib/snapConstants";
import type {
  SnapBoardInfo,
  SnapBoardReadResponse,
  SnapInputInfo,
  SnapLiveResponse,
} from "../../lib/types";

export interface PanelData {
  input: SnapInputInfo;
  freqMhz: number[];
  values: (number | null)[] | null;
  /** ADC RMS for the title suffix; only set in the board layer. */
  rms: number | null;
}

export interface BoardSectionProps {
  board: SnapBoardInfo;
  read: SnapBoardReadResponse | null;
  live: SnapLiveResponse | null;
  panels: PanelData[];
  units: "dB" | "linear";
  layer: "correlator" | "board";
  onSelect: (adc: number) => void;
}

/**
 * One board: a muted line of facts, any problem as its own alert sentence,
 * then that board's inputs as an equal-panel grid ordered by antenna number.
 */
export default function BoardSection({
  board,
  read,
  live,
  panels,
  units,
  layer,
  onSelect,
}: BoardSectionProps) {
  const cols = useColumns();
  const dark = layer === "correlator" ? darkSubbands(live) : [];
  const problems = boardProblems(board, read, live, CORR_AGE_STALE_S);

  return (
    <section className="board">
      <p className="board__line">{boardLine(board, read, board.inputs ?? [])}</p>
      {problems.map((text) => (
        <p key={text} className="board__problem">
          {text}
        </p>
      ))}
      <div className="grid">
        {panels.map((panel, i) => (
          <SpectrumPanel
            key={panel.input.adc}
            title={panelTitle(board, panel.input, panel.rms)}
            inBf={panel.input.in_bf}
            freqMhz={panel.freqMhz}
            values={panel.values}
            units={units}
            layer={layer}
            darkSubbands={dark}
            showXTicks={isBottomRow(i, panels.length, cols)}
            showYTicks={isLeftColumn(i, cols)}
            onClick={() => onSelect(panel.input.adc)}
          />
        ))}
      </div>
    </section>
  );
}
