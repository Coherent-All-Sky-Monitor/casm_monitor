export interface SearchBand {nchan:number;low_mhz:number;high_mhz:number}
export interface StaticMask {enabled:boolean|null;first?:number;last?:number}
export interface MaskSegment {
  masked:boolean;first:number;last:number;count:number;
  low:number;high:number;fraction:number;
}

/** Display native descending channels left-to-right in increasing frequency. */
export function maskSegments(band:SearchBand|undefined,mask:StaticMask|undefined):MaskSegment[]|null {
  if(!band||!mask||typeof mask.enabled!=='boolean'||!Number.isInteger(band.nchan)||band.nchan<2
    ||!Number.isFinite(band.low_mhz)||!Number.isFinite(band.high_mhz)||band.high_mhz<=band.low_mhz)return null;
  const n=band.nchan;
  const segment=(first:number,last:number,masked:boolean):MaskSegment=>{
    const step=(band.high_mhz-band.low_mhz)/(n-1),count=last-first+1;
    return {first,last,masked,count,low:band.high_mhz-last*step,high:band.high_mhz-first*step,fraction:count/n};
  };
  if(!mask.enabled)return [segment(0,n-1,false)];
  const {first,last}=mask;
  if(first==null||last==null||!Number.isInteger(first)||!Number.isInteger(last)||first<0||last<first||last>=n)return null;
  return [last<n-1?segment(last+1,n-1,false):null,segment(first,last,true),first>0?segment(0,first-1,false):null]
    .filter((s):s is MaskSegment=>s!==null);
}
