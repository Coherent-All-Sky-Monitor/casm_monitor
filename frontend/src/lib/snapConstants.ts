// Static layout facts for the SNAPs tab (docs/plan.md section 1, M1).
// The board *inventory* (feng_id, wiring) comes from the backend
// (/api/snaps/boards); these are just the fixed constants needed to lay the
// UI out before that response lands and to interpret it (subband centres,
// the correlator passband, RMS health bands).

/** Antenna boards, in feng_id 0..3 display order. */
export const ANTENNA_IPS = [
  "192.168.120.52",
  "192.168.120.51",
  "192.168.120.62",
  "192.168.120.73",
];

/** Antenna-less relay boards that only carry PPS through the timing chain. */
export const RELAY_IPS = ["192.168.120.59", "192.168.120.68", "192.168.120.69"];

/** Subband 0..5 centre frequencies (MHz), corr1 0-2 / corr2 3-5. */
export const SUBBAND_CENTERS_MHZ = [476.3, 460.6, 445.3, 429.7, 414.1, 398.4];

/** Correlator (Kafka bandpass) passband, MHz. */
export const CORR_BAND_MHZ: [number, number] = [390.6, 484.4];

/** Board-side read passband, MHz. */
export const BOARD_BAND_MHZ: [number, number] = [375, 500];

/** Healthy ADC RMS band (LSB). */
export const ADC_RMS_HEALTHY: [number, number] = [5, 30];

/** Target ADC RMS band (LSB). */
export const ADC_RMS_TARGET: [number, number] = [8, 10];

/** Age (s) thresholds for the correlator-bandpass badge. The live frame is
 * intrinsically 30-100 s old (corr2 producers publish ~96 s after the frame
 * timestamp, docs/notes/kafka-bandpass-schema.md), so warn/stale are set well
 * above that: warn > 3 min, stale > 10 min (2026-09-08 integration pass). */
export const CORR_AGE_WARN_S = 180;
export const CORR_AGE_STALE_S = 10 * 60;

/** Age (s) thresholds for the board-read badge: warn > 2h, stale > 6h. */
export const BOARD_AGE_WARN_S = 2 * 3600;
export const BOARD_AGE_STALE_S = 6 * 3600;

/** Manual "read boards now" minimum spacing, mirrored here only for the
 * fallback countdown shown before the first 429 tells us the real value. */
export const DEFAULT_READ_RETRY_S = 5 * 60;

/** ``eq_epoch`` is a 12-hex-digit sha256 prefix over a board's EQ
 * coefficients + FFT shift, NOT a timestamp (kafka-bandpass-schema.md,
 * 2026-09-08): render it as an opaque tag, e.g. "EQ epoch a12aaaaeffdf",
 * never parsed/formatted as a date. */
export function formatEqEpoch(eqEpoch: string | null): string {
  return eqEpoch ? `EQ epoch ${eqEpoch}` : "EQ epoch —";
}

export type AgeState = "ok" | "warn" | "stale";

export function ageState(ageS: number | null, warnS: number, staleS: number): AgeState {
  if (ageS === null) return "stale";
  if (ageS > staleS) return "stale";
  if (ageS > warnS) return "warn";
  return "ok";
}

export function formatAge(ageS: number | null): string {
  if (ageS === null) return "never";
  if (ageS < 60) return `${Math.round(ageS)} s ago`;
  if (ageS < 3600) return `${Math.round(ageS / 60)} min ago`;
  if (ageS < 86400) return `${(ageS / 3600).toFixed(1)} h ago`;
  return `${Math.round(ageS / 86400)} d ago`;
}
