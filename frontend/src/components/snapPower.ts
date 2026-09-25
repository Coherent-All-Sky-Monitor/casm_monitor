/** Display-only reference. Never subtract a per-input or per-date noise floor. */
export const SNAP_REFERENCE_POWER = 1e-10;
export const SNAP_FREQ_LIMITS:[number,number] = [374.9,500.1];
export const dbOffset = (reference:number) => -10*Math.log10(reference);

/** Focus on the bandpass, with explicit clipped-peak markers in SnapPlot.
 * Limits are computed from a pinned snapshot, not refitted while browsing dates.
 */
export function snapRange(values:(number|null)[], reference:number, full=false):[number,number] {
  const finite=values.filter((v):v is number=>v!==null&&Number.isFinite(v)).sort((a,b)=>a-b);
  const offset=dbOffset(reference);
  if(!finite.length)return [-95+offset,-55+offset];
  const at=(q:number)=>finite[Math.floor((finite.length-1)*q)]+offset;
  let lo=at(full?0:.01),hi=at(full?1:.99);
  const span=Math.max(hi-lo,10),pad=Math.max(1.5,span*.08);
  const mid=(lo+hi)/2;lo=Math.min(lo,mid-5)-pad;hi=Math.max(hi,mid+5)+pad;
  return [Math.floor(lo/2.5)*2.5,Math.ceil(hi/2.5)*2.5];
}
