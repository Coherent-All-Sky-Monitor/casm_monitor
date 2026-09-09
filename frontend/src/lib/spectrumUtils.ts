// Small numeric helpers shared by the SNAPs card grid, expanded panel and
// waterfall. Server-side decimation (nchan=, max_cells=) is the primary
// mechanism (docs/plan.md "server-side decimation"); this client-side
// downsample only covers the case in the M1 spec where the server sends
// more than a small card tile needs.

/** Bin `values` (aligned to `freq`) down to at most `maxPoints` bins, each
 * bin's value the mean of its members (NaN/undefined skipped). Returns
 * parallel arrays of the same reduced length. A no-op if already short
 * enough. */
export function downsample(
  freq: number[],
  values: (number | null)[],
  maxPoints: number,
): { freq: number[]; values: (number | null)[] } {
  const n = freq.length;
  if (n <= maxPoints || maxPoints <= 0) return { freq, values };
  const binSize = n / maxPoints;
  const outFreq: number[] = [];
  const outValues: (number | null)[] = [];
  for (let b = 0; b < maxPoints; b++) {
    const start = Math.floor(b * binSize);
    const end = Math.max(start + 1, Math.floor((b + 1) * binSize));
    let sum = 0;
    let count = 0;
    let fsum = 0;
    for (let i = start; i < end && i < n; i++) {
      fsum += freq[i];
      const v = values[i];
      if (v !== null && v !== undefined && !Number.isNaN(v)) {
        sum += v;
        count += 1;
      }
    }
    const span = Math.min(end, n) - start;
    outFreq.push(span > 0 ? fsum / span : freq[Math.min(start, n - 1)]);
    outValues.push(count > 0 ? sum / count : null);
  }
  return { freq: outFreq, values: outValues };
}

/** dB <-> linear power conversion for the units toggle. */
export function dbToLinear(db: number | null): number | null {
  if (db === null || Number.isNaN(db)) return null;
  return 10 ** (db / 10);
}

export function convertSeries(
  values: (number | null)[],
  units: "dB" | "linear",
): (number | null)[] {
  if (units === "dB") return values;
  return values.map(dbToLinear);
}

/** True if a trace is entirely null/NaN (never-collected input). */
export function isAllNull(values: (number | null)[] | null | undefined): boolean {
  if (!values) return true;
  return values.every((v) => v === null || v === undefined || Number.isNaN(v));
}

/** Coarse diagnosis per docs/plan.md's legend, from a single spectrum. Best
 * effort / illustrative only — the real diagnosis lives server-side per the
 * Events tab step detector; this just labels the expanded panel. */
export function diagnose(values: (number | null)[] | null | undefined): string | null {
  if (isAllNull(values)) return null;
  const nums = (values as number[]).filter((v) => v !== null && !Number.isNaN(v));
  if (nums.length === 0) return null;
  const min = Math.min(...nums);
  const spread = Math.max(...nums) - min;
  const mean = nums.reduce((a, b) => a + b, 0) / nums.length;
  if (spread < 0.6) {
    return mean < -40 ? "flat, low level — likely dead feed" : "flat, pinned constant — likely railed ADC";
  }
  return null;
}
