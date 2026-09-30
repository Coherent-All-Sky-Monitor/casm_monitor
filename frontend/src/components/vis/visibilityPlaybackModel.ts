import type {Antenna, Quantity} from './ArrayPlots';

export type MovieManifest = {
  id:string;version:number;selected_inputs:number[];inputs:Antenna[];pairs:[number,number][];
  times:number[];lst:(string|null)[];first_frame:number;freq_mhz:number[];preview_freq_mhz:number[];
  start:number;end:number;window_s:number;integration_s:number;chunk_size:number;
  selection:{mode:string;reference_input:number;reference:string;span_days:number;window_hours:number;end:number;fmin:number;fmax:number};
  limits:{spectrum:Record<Quantity|'amp_log',[number,number]>;dynamic:Record<Quantity,[number,number]>}[];
  palettes:Record<string,number[][]>;availability:{start:number;end:number};
};
export type MovieChunk={base:number;count:number;pairs:number;channels:number;components:number;values:Float32Array;times:number[]};
export type Encoding='complex'|'preview';

export const VISIBILITY_SPEEDS=[.5,1,2,4,8,16,32] as const;
/** Nominal wall time; buffering and slow rendering can extend it. */
export function visibilityDuration(frames:number,speed:number){
  const seconds=Math.ceil(Math.max(0,frames)/speed),minutes=Math.floor(seconds/60);
  return minutes?`${minutes}m ${String(seconds%60).padStart(2,'0')}s`:`${seconds}s`;
}

export function lowerBound(times:number[],t:number){
  let lo=0,hi=times.length;while(lo<hi){const mid=(lo+hi)>>>1;if(times[mid]<t)lo=mid+1;else hi=mid;}return lo;
}
export function windowFirst(m:MovieManifest,index:number){return lowerBound(m.times,m.times[index]-m.window_s);}
export function decodeVisibilityChunk(buffer:ArrayBuffer,m:MovieManifest,base:number,encoding:Encoding):MovieChunk{
  if(buffer.byteLength<4)throw new Error('Incomplete visibility chunk');
  const size=new DataView(buffer).getUint32(0,true),offset=4+size;
  if(offset>buffer.byteLength||offset%4)throw new Error('Invalid visibility header');
  const h=JSON.parse(new TextDecoder().decode(new Uint8Array(buffer,4,size)));
  const channels=encoding==='complex'?m.freq_mhz.length:m.preview_freq_mhz.length,components=encoding==='complex'?2:3;
  const count=Math.min(m.chunk_size,m.times.length-base);
  if(h.version!==1||h.id!==m.id||h.base!==base||h.encoding!==encoding||h.dtype!=='f32le'
    ||JSON.stringify(h.shape)!==JSON.stringify([count,m.pairs.length,channels,components])
    ||JSON.stringify(h.times)!==JSON.stringify(m.times.slice(base,base+count))
    ||JSON.stringify(h.pairs)!==JSON.stringify(m.pairs)
    ||buffer.byteLength-offset!==count*m.pairs.length*channels*components*4)
    throw new Error('Visibility chunk does not match this saved sequence');
  if(new Uint8Array(new Uint32Array([1]).buffer)[0]!==1)throw new Error('Little-endian float support required');
  return {base,count,pairs:m.pairs.length,channels,components,times:h.times,values:new Float32Array(buffer,offset)};
}
export function component(real:number,imag:number,q:Quantity,amp=Math.hypot(real,imag)){
  if(!Number.isFinite(real)||!Number.isFinite(imag))return null;
  return q==='amp'?amp:q==='real'?real:q==='imag'?imag:real===0&&imag===0?null:Math.atan2(imag,real);
}
export function chunkValue(c:MovieChunk,index:number,pair:number,channel:number,q:Quantity){
  const k=(((index-c.base)*c.pairs+pair)*c.channels+channel)*c.components;
  return component(c.values[k],c.values[k+1],q,c.components===3?c.values[k+2]:undefined);
}

/** Three running sums preserve mean |V| separately from the complex mean. */
export class RollingSpectra {
  private first=-1;private last=-1;private sums:Float64Array;private counts:Uint32Array;
  constructor(private pairs:number,private channels:number){this.sums=new Float64Array(pairs*channels*3);this.counts=new Uint32Array(pairs*channels);}
  update(first:number,last:number,get:(i:number)=>MovieChunk|undefined){
    const adjust=(i:number,sign:number)=>{
      const c=get(i);if(!c)throw new Error('Window is not buffered');
      const start=(i-c.base)*this.pairs*this.channels*2;
      for(let k=0;k<this.counts.length;k++){
        const re=c.values[start+2*k],im=c.values[start+2*k+1];
        if(!Number.isFinite(re)||!Number.isFinite(im))continue;
        this.counts[k]+=sign;this.sums[3*k]+=sign*re;this.sums[3*k+1]+=sign*im;this.sums[3*k+2]+=sign*Math.hypot(re,im);
        if(!this.counts[k])this.sums.fill(0,3*k,3*k+3);
      }
    };
    if(last===this.last+1&&first>=this.first&&first<=this.last+1&&get(this.first)){
      for(let i=this.first;i<first;i++)adjust(i,-1);adjust(last,1);
    }else if(first!==this.first||last!==this.last){
      this.sums.fill(0);this.counts.fill(0);for(let i=first;i<=last;i++)adjust(i,1);
    }
    this.first=first;this.last=last;
  }
  spectrum(pair:number,q:Quantity){
    return Array.from({length:this.channels},(_,ch)=>{
      const k=pair*this.channels+ch,n=this.counts[k];
      return n?component(this.sums[k*3]/n,this.sums[k*3+1]/n,q,this.sums[k*3+2]/n):null;
    });
  }
}

export function movieOrder(inputs:Antenna[],selected:number[],order:string){
  return inputs.filter(a=>selected.includes(a.packet_idx)).sort((a,b)=>order==='snap'
    ?(a.snap??Infinity)-(b.snap??Infinity)||(a.slot??'').localeCompare(b.slot??'')||(a.adc??Infinity)-(b.adc??Infinity)||a.packet_idx-b.packet_idx
    :b.position_enu_m[1]-a.position_enu_m[1]||a.position_enu_m[0]-b.position_enu_m[0]);
}
