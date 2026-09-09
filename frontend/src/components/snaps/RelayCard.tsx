import { ageState, formatAge, BOARD_AGE_WARN_S, BOARD_AGE_STALE_S } from "../../lib/snapConstants";
import type { SnapBoardInfo, SnapBoardReadResponse } from "../../lib/types";

export interface RelayCardProps {
  board: SnapBoardInfo;
  read: SnapBoardReadResponse | null;
}

/** Antenna-less relay board (.59 .68 .69): PPS/programmed status only, per
 * docs/plan.md "plus relays .59 .68 .69 shown as PPS-only". */
export default function RelayCard({ board, read }: RelayCardProps) {
  const ageS = read?.age_s ?? null;
  const state = ageState(ageS, BOARD_AGE_WARN_S, BOARD_AGE_STALE_S);
  const pps = read?.pps;
  const ppsState = pps?.ok === true ? "ok" : pps?.ok === false ? "stale" : "warn";

  return (
    <div className="snap-card snap-card--relay">
      <div className="snap-card__header">
        <span className="snap-card__title">SNAP {board.ip} | relay (no ADC inputs)</span>
      </div>
      <div className="snap-card__badges">
        <span className={`chip state-${state}`} title="board read">
          <span className="chip-label">board read</span>
          <span className="chip-value">{formatAge(ageS)}</span>
        </span>
        <span className={`chip state-${ppsState}`} title={pps?.detail ?? "PPS status unknown"}>
          <span className="chip-label">PPS</span>
          <span className="chip-value">{pps?.ok === null || pps?.ok === undefined ? "unknown" : pps.ok ? "locked" : "no PPS"}</span>
        </span>
        <span className={`chip state-${read?.programmed ? "ok" : "warn"}`}>
          <span className="chip-label">programmed</span>
          <span className="chip-value">
            {read?.programmed === null || read?.programmed === undefined ? "unknown" : read.programmed ? "yes" : "no"}
          </span>
        </span>
      </div>
      <p className="snap-card__relay-note">
        Relay board: no antenna inputs, sits in the PPS timing path only. A
        golden-image board cannot report PPS at all — "no PPS" here does not
        mean the chain skips this slot.
      </p>
    </div>
  );
}
