import ToggleBar from "../ToggleBar";
import SpectrumTile from "./SpectrumTile";
import AdcRmsBar from "./AdcRmsBar";
import SubbandStrip from "./SubbandStrip";
import { useUrlParam } from "../../lib/useUrlParam";
import {
  ageState,
  formatAge,
  formatEqEpoch,
  CORR_AGE_WARN_S,
  CORR_AGE_STALE_S,
  BOARD_AGE_WARN_S,
  BOARD_AGE_STALE_S,
  CORR_BAND_MHZ,
} from "../../lib/snapConstants";
import type { SnapBoardInfo } from "../../lib/types";

export interface BoardCardTileData {
  adc: number;
  label: string;
  freqMhz: number[];
  values: (number | null)[] | null;
  bold: boolean;
  dimmed: boolean;
  mismatch?: boolean;
}

export interface BoardCardProps {
  board: SnapBoardInfo;
  units: "dB" | "linear";
  correlatorAgeS: number | null;
  boardAgeS: number | null;
  correlatorTiles: BoardCardTileData[];
  boardTiles: BoardCardTileData[];
  adcRms: (number | null)[] | null;
  subbandsOk: boolean[] | null;
  fengIdHw: number | null;
  eqEpoch: string | null;
  onReadNow: () => void;
  readDisabled: boolean;
  readCountdownS: number | null;
  reading: boolean;
  onExpandTile: (adc: number) => void;
  historyMode: boolean;
}

function AgeBadge({
  label,
  ageS,
  warnS,
  staleS,
}: {
  label: string;
  ageS: number | null;
  warnS: number;
  staleS: number;
}) {
  const state = ageState(ageS, warnS, staleS);
  return (
    <span className={`chip state-${state}`} title={label}>
      <span className="chip-label">{label}</span>
      <span className="chip-value">{formatAge(ageS)}</span>
    </span>
  );
}

/**
 * One antenna SNAP card: header, age badges, the correlator|board layer
 * toggle (URL-backed, keyed per board so each card is independent), the
 * 3x4 spectrum tile grid, the ADC RMS row (board layer only) and the
 * subband strip. docs/plan.md section "1. SNAPs".
 */
export default function BoardCard({
  board,
  units,
  correlatorAgeS,
  boardAgeS,
  correlatorTiles,
  boardTiles,
  adcRms,
  subbandsOk,
  fengIdHw,
  eqEpoch,
  onReadNow,
  readDisabled,
  readCountdownS,
  reading,
  onExpandTile,
  historyMode,
}: BoardCardProps) {
  const layerParamKey = `layer_${board.ip.split(".").pop()}`;
  const [layer] = useUrlParam(layerParamKey, "correlator");
  const active = layer === "board" ? boardTiles : correlatorTiles;
  const shadeBand: [number, number] | undefined = layer === "board" ? CORR_BAND_MHZ : undefined;

  return (
    <div className="snap-card">
      <div className="snap-card__header">
        <span className="snap-card__title">
          SNAP {board.ip} | feng {board.feng_id ?? "?"}
          {fengIdHw !== null && fengIdHw !== board.feng_id ? ` (hw ${fengIdHw})` : ""} | slot {board.slot}
        </span>
        <SubbandStrip subbandsOk={subbandsOk} />
      </div>
      <div className="snap-card__badges">
        <AgeBadge
          label="correlator bandpass"
          ageS={correlatorAgeS}
          warnS={CORR_AGE_WARN_S}
          staleS={CORR_AGE_STALE_S}
        />
        <AgeBadge label="board read" ageS={boardAgeS} warnS={BOARD_AGE_WARN_S} staleS={BOARD_AGE_STALE_S} />
        <span className="chip" title="opaque hash over the board's EQ coefficients + FFT shift, not a timestamp">
          <span className="chip-value">{formatEqEpoch(eqEpoch)}</span>
        </span>
        {!historyMode && (
          <button className="snap-read-btn" onClick={onReadNow} disabled={readDisabled || reading}>
            {reading ? "reading…" : readCountdownS ? `read boards now (${readCountdownS}s)` : "read boards now"}
          </button>
        )}
      </div>
      <ToggleBar
        paramKey={layerParamKey}
        defaultValue="correlator"
        options={[
          { value: "correlator", label: "correlator" },
          { value: "board", label: "board" },
        ]}
      />
      <div className="snap-tile-grid">
        {active.map((tile) => (
          <SpectrumTile
            key={tile.adc}
            title={tile.label}
            freqMhz={tile.freqMhz}
            values={tile.values}
            units={units}
            shadeBand={shadeBand}
            bold={tile.bold}
            dimmed={tile.dimmed}
            mismatch={tile.mismatch}
            onClick={() => onExpandTile(tile.adc)}
          />
        ))}
      </div>
      {layer === "board" && adcRms && (
        <div className="snap-rms-row">
          {adcRms.map((rms, adc) => (
            <AdcRmsBar key={adc} rms={rms} />
          ))}
        </div>
      )}
    </div>
  );
}
