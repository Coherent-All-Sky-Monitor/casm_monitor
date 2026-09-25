/** Display-only reference. Never subtract a per-input or per-date noise floor. */
export const SNAP_REFERENCE_POWER = 1e-10;
export const SNAP_FREQ_LIMITS:[number,number] = [374.9,500.1];
export const dbOffset = (reference:number) => -10*Math.log10(reference);

/** Full finite data range plus padding; never trim channels by percentile. */
export function snapRange(values:(number|null)[], reference:number):[number,number] {
  const offset=dbOffset(reference);
  let lo=Infinity,hi=-Infinity;
  for(const value of values)if(value!==null&&Number.isFinite(value)) {
    lo=Math.min(lo,value+offset);hi=Math.max(hi,value+offset);
  }
  if(!Number.isFinite(lo))return [-95+offset,-55+offset];
  const span=Math.max(hi-lo,10),pad=Math.max(1.5,span*.08);
  const mid=(lo+hi)/2;lo=Math.min(lo,mid-5)-pad;hi=Math.max(hi,mid+5)+pad;
  return [Math.floor(lo/2.5)*2.5,Math.ceil(hi/2.5)*2.5];
}
