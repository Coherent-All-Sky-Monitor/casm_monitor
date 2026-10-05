/** Display units only. Average/subtract native power before converting to dB. */
export type PowerScale = 'db' | 'linear';
export function powerValue(value:number|null,scale:PowerScale,reference=1):number|null {
  if(value===null||!Number.isFinite(value))return null;
  if(scale==='linear')return value;
  return value>0&&reference>0?10*(Math.log10(value)-Math.log10(reference)):null;
}
export function powerLabel(scale:PowerScale,unit='counts'){
  return scale==='linear'?`Power (${unit})`:'Power (dB)';
}
export function linearPowerRange(values:(number|null)[]):[number,number] {
  let lo=Infinity,hi=-Infinity;
  for(const v of values)if(v!==null&&Number.isFinite(v)){lo=Math.min(lo,v);hi=Math.max(hi,v);}
  if(!Number.isFinite(lo+hi)||(lo===0&&hi===0))return [0,1];
  const pad=Math.max((hi-lo)*.06,Math.abs(hi)*.015,Number.MIN_VALUE);
  return [lo>=0?Math.max(0,lo-pad):lo-pad,hi+pad];
}
