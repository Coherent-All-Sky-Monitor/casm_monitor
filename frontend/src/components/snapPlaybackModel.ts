/** Playback advances saved reads, never synthesizes missing hours. */
export type PlaybackTimeZone = 'UTC' | 'America/Los_Angeles';
export const PLAYBACK_BATCH_SIZE=12;

/** Compact transport only; all 4096 native channels remain separate. */
export function decodePlaybackBatch(buffer:ArrayBuffer,keys:string[]):Record<string,any>[] {
  if(buffer.byteLength<4)throw new Error('Incomplete playback batch');
  const headerBytes=new DataView(buffer).getUint32(0,true),offset=4+headerBytes;
  if(offset>buffer.byteLength||offset%4)throw new Error('Invalid playback header');
  const meta=JSON.parse(new TextDecoder().decode(new Uint8Array(buffer,4,headerBytes)));
  if(meta.version!==1||meta.encoding!=='f32le'||JSON.stringify(meta.inputs)!==JSON.stringify(keys)
      ||meta.shape?.[1]!==keys.length||meta.shape?.[2]!==4096||meta.shape?.[0]!==meta.frames?.length
      ||meta.shape[0]<1||meta.shape[0]>PLAYBACK_BATCH_SIZE
      ||meta.freq_mhz?.length!==4096||!meta.freq_mhz.every(Number.isFinite)
      ||buffer.byteLength-offset!==meta.shape[0]*keys.length*4096*4)throw new Error('Playback batch does not match the requested frames');
  const floats=new Float32Array(buffer,offset);
  if(new Uint8Array(new Uint32Array([1]).buffer)[0]!==1)throw new Error('Playback requires little-endian float support');
  return meta.frames.map((frame:Record<string,any>,f:number)=>{
    const boards=frame.boards.map((b:Record<string,any>)=>({...b,spectra_db:{}}));
    keys.forEach((key,i)=>{
      const [ip,adc]=key.split('/'),board=boards.find((b:Record<string,any>)=>b.ip===ip);
      if(!board)throw new Error('Playback board metadata is missing');
      const start=(f*keys.length+i)*4096;
      board.spectra_db[adc]=board.ts===null||board.error?null:Array.from(floats.subarray(start,start+4096),v=>Number.isFinite(v)?v:null);
    });
    return {boards,freq_mhz:meta.freq_mhz,ids:frame.ids};
  });
}
export function playbackStamp(ts:number, zone:PlaybackTimeZone):string {
  const parts=new Intl.DateTimeFormat('en-CA',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23',timeZoneName:'short'}).formatToParts(new Date(ts*1000));
  const part=(type:string)=>parts.find(p=>p.type===type)?.value??'';
  return `${part('year')}-${part('month')}-${part('day')}  ${part('hour')}:${part('minute')}:${part('second')} ${part('timeZoneName')}`;
}
export function playbackNext(index:number,count:number,loop:boolean):number|null {
  return count<2?null:index+1<count?index+1:loop?0:null;
}
export function playbackGap(previous:number,current:number,interval:number):string|null {
  const minutes=Math.round((current-previous)/60);
  return current-previous>interval*1.5?`Gap between reads: ${minutes>=60?`${(minutes/60).toFixed(1)} h`:`${minutes} min`}`:null;
}
export function playbackColumns(count:number,width:number,height:number):number {
  let columns=1,best=Infinity;
  for(let cols=1;cols<=Math.min(count,Math.max(1,Math.floor(width/225)));cols++){
    const rows=Math.ceil(count/cols),w=(width-(cols-1)*8)/cols,h=(height-(rows-1)*8)/rows;
    const score=Math.abs(Math.log(w/Math.max(155,h)/1.7))+(rows*cols-count)/count+Math.max(0,155-h)/60;
    if(score<best){best=score;columns=cols;}
  }
  return columns;
}
