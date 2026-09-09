import { ADC_RMS_HEALTHY, ADC_RMS_TARGET } from "../../lib/snapConstants";

export interface AdcRmsBarProps {
  rms: number | null;
  maxScale?: number;
}

/** Small horizontal bar for one ADC's RMS (LSB), with the 5-30 healthy band
 * shaded and the 8-10 target band marked, per docs/plan.md's board layer
 * spec. */
export default function AdcRmsBar({ rms, maxScale = 40 }: AdcRmsBarProps) {
  const pct = (v: number) => `${Math.min(100, Math.max(0, (v / maxScale) * 100))}%`;
  const inHealthy = rms !== null && rms >= ADC_RMS_HEALTHY[0] && rms <= ADC_RMS_HEALTHY[1];
  return (
    <div className="snap-rms-bar" title={rms === null ? "RMS unknown" : `ADC RMS ${rms.toFixed(1)} LSB`}>
      <div
        className="snap-rms-bar__healthy"
        style={{ left: pct(ADC_RMS_HEALTHY[0]), width: `calc(${pct(ADC_RMS_HEALTHY[1])} - ${pct(ADC_RMS_HEALTHY[0])})` }}
      />
      <div
        className="snap-rms-bar__target"
        style={{ left: pct(ADC_RMS_TARGET[0]), width: `calc(${pct(ADC_RMS_TARGET[1])} - ${pct(ADC_RMS_TARGET[0])})` }}
      />
      {rms !== null && (
        <div
          className={`snap-rms-bar__marker${inHealthy ? "" : " snap-rms-bar__marker--bad"}`}
          style={{ left: pct(rms) }}
        />
      )}
      <span className="snap-rms-bar__value">{rms === null ? "—" : rms.toFixed(1)}</span>
    </div>
  );
}
