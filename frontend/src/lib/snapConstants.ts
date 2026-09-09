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

/** Half-width of one subband (MHz): the six centres are evenly spaced. */
const SUBBAND_HALF_MHZ = (SUBBAND_CENTERS_MHZ[0] - SUBBAND_CENTERS_MHZ[5]) / 5 / 2;

/** Subband 0..5 [low, high] edges (MHz), used to draw a dark subband as a
 * grey span across a spectrum panel instead of a coloured strip beside it. */
export const SUBBAND_EDGES_MHZ: [number, number][] = SUBBAND_CENTERS_MHZ.map(
  (c) => [c - SUBBAND_HALF_MHZ, c + SUBBAND_HALF_MHZ] as [number, number],
);

/** Correlator (Kafka bandpass) passband, MHz. */
export const CORR_BAND_MHZ: [number, number] = [390.6, 484.4];

/** Board-side read passband, MHz. */
export const BOARD_BAND_MHZ: [number, number] = [375, 500];

/** Healthy ADC RMS band (LSB). */
export const ADC_RMS_HEALTHY: [number, number] = [5, 30];

/** Age (s) past which the correlator layer is worth a sentence of its own.
 * The live frame is intrinsically 30-100 s old (corr2 producers publish ~96 s
 * after the frame timestamp, docs/notes/kafka-bandpass-schema.md), so this
 * sits well above that at 10 min (2026-09-08 integration pass). */
export const CORR_AGE_STALE_S = 10 * 60;

export function formatAge(ageS: number | null): string {
  if (ageS === null) return "never";
  if (ageS < 60) return `${Math.round(ageS)} s ago`;
  if (ageS < 3600) return `${Math.round(ageS / 60)} min ago`;
  if (ageS < 86400) return `${(ageS / 3600).toFixed(1)} h ago`;
  return `${Math.round(ageS / 86400)} d ago`;
}
