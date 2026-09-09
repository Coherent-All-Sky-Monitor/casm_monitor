import { SUBBAND_CENTERS_MHZ } from "../../lib/snapConstants";

export interface SubbandStripProps {
  subbandsOk: boolean[] | null;
}

/** Six-cell sb0..sb5 indicator, colored by subbands_ok (docs/plan.md "missing
 * subband panel" precursor for the card view). */
export default function SubbandStrip({ subbandsOk }: SubbandStripProps) {
  return (
    <div className="snap-subband-strip" title="subband delivery (correlator-side)">
      {SUBBAND_CENTERS_MHZ.map((centre, i) => {
        const ok = subbandsOk ? subbandsOk[i] : null;
        const cls = ok === null ? "unknown" : ok ? "ok" : "bad";
        return (
          <span
            key={i}
            className={`snap-subband-cell snap-subband-cell--${cls}`}
            title={`sb${i} (${centre.toFixed(1)} MHz): ${ok === null ? "unknown" : ok ? "ok" : "missing/all-zero"}`}
          >
            sb{i}
          </span>
        );
      })}
    </div>
  );
}
