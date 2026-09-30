import {useEffect,useRef,useState} from 'react';
import {decodeVisibilityChunk,Encoding,MovieChunk,MovieManifest,windowFirst} from './visibilityPlaybackModel';

const MAX_BYTES=384*1024**2;
/** Byte-bounded session cache, including recently selected references. */
export class MovieBuffer {
  chunks=new Map<string,MovieChunk>();bytes=0;
  key(m:MovieManifest,base:number,e:Encoding){return `${m.id}/${e}/${base}`;}
  get(m:MovieManifest,i:number,e:Encoding){return this.chunks.get(this.key(m,Math.floor(i/m.chunk_size)*m.chunk_size,e));}
  put(m:MovieManifest,c:MovieChunk,e:Encoding,keep:Set<string>){
    const key=this.key(m,c.base,e);const old=this.chunks.get(key);if(old)this.bytes-=old.values.byteLength;
    this.chunks.delete(key);this.chunks.set(key,c);this.bytes+=c.values.byteLength;
    for(const [k,v] of this.chunks){if(this.bytes<=MAX_BYTES)break;if(!keep.has(k)){this.chunks.delete(k);this.bytes-=v.values.byteLength;}}
  }
}

export function useVisibilityFrames(m:MovieManifest|null,index:number,encoding:Encoding,windowed:boolean,loop:boolean,cache:MovieBuffer){
  const [revision,render]=useState(0),[error,setError]=useState(''),[attempt,setAttempt]=useState(0);
  const pending=useRef(new Map<string,{controller:AbortController;promise:Promise<void>}>());
  const first=m?(windowed?windowFirst(m,index):index):0,base=m?Math.floor(index/m.chunk_size)*m.chunk_size:0;
  const start=m?Math.floor(first/m.chunk_size)*m.chunk_size:0;
  const needed:number[]=[],wanted:number[]=[];
  if(m){
    for(let b=start;b<=base;b+=m.chunk_size)needed.push(b);
    wanted.push(...needed);
    for(let b=base+m.chunk_size;b<Math.min(m.times.length,base+3*m.chunk_size);b+=m.chunk_size)wanted.push(b);
    if(loop&&base+3*m.chunk_size>=m.times.length){
      const lo=windowed?windowFirst(m,m.first_frame):m.first_frame;
      for(let b=Math.floor(lo/m.chunk_size)*m.chunk_size;b<=Math.floor(m.first_frame/m.chunk_size)*m.chunk_size;b+=m.chunk_size)if(!wanted.includes(b))wanted.push(b);
    }
  }
  const signature=wanted.join(','),required=needed.join(',');
  const wantedNow=useRef(new Set<string>());
  wantedNow.current=new Set(m?wanted.map(b=>cache.key(m,b,encoding)):[]);
  const keep=new Set(m?needed.map(b=>cache.key(m,b,encoding)):[]);
  const keepNow=useRef(keep);keepNow.current=keep;
  useEffect(()=>()=>{for(const p of pending.current.values())p.controller.abort();pending.current.clear();},[]);
  useEffect(()=>{
    let active=true;setError('');if(!m)return;
    for(const [key,p] of pending.current)if(!wantedNow.current.has(key)){p.controller.abort();pending.current.delete(key);}
    const cellsPerFrame=m.pairs.length*(encoding==='complex'?m.freq_mhz.length*2:m.preview_freq_mhz.length*3);
    if(needed.length*m.chunk_size*cellsPerFrame*4>MAX_BYTES){setError('This window is too large for the browser buffer. Select fewer antennas or a shorter panel window.');return;}
    const load=async(b:number)=>{
      if(cache.get(m,b,encoding))return;
      const key=cache.key(m,b,encoding),existing=pending.current.get(key);if(existing)return existing.promise;
      const controller=new AbortController();
      const promise=fetch(`/api/science/array/playback-chunk?${new URLSearchParams({id:m.id,base:String(b),encoding})}`,{signal:controller.signal}).then(async r=>{
        if(!r.ok){let message=`Saved chunk unavailable (${r.status})`;try{message=(await r.json()).detail??message;}catch{}throw new Error(message);}
        const data=decodeVisibilityChunk(await r.arrayBuffer(),m,b,encoding);
        if(!controller.signal.aborted){cache.put(m,data,encoding,keepNow.current);render(v=>v+1);}
      });
      pending.current.set(key,{controller,promise});
      promise.finally(()=>{if(pending.current.get(key)?.promise===promise)pending.current.delete(key);}).catch(()=>{});
      return promise;
    };
    // Two bounded requests at a time. Existing prefetch survives a frame change.
    let cursor=0;
    const work=async()=>{
      while(active&&cursor<wanted.length){const b=wanted[cursor++];try{await load(b);}catch(e){if(active&&needed.includes(b))setError((e as Error).message);return;}}
    };
    void work();void work();return()=>{active=false;};
  },[m,signature,required,encoding,attempt,cache]);
  const ready=!!m&&!error&&needed.every(b=>!!cache.get(m,b,encoding));
  return {ready,error,revision,loaded:needed.filter(b=>m&&cache.get(m,b,encoding)).length,total:needed.length,
    retry:()=>setAttempt(v=>v+1),get:(i:number)=>m?cache.get(m,i,encoding):undefined};
}
