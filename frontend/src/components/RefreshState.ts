import {useEffect, useState} from 'react';

/** Fast background reads stay visually silent. Never delays the data itself. */
export function useDelayedBusy(busy:boolean, delay=500) {
  const [visible,setVisible]=useState(false);
  useEffect(()=>{
    if(!busy){setVisible(false);return;}
    const timer=window.setTimeout(()=>setVisible(true),delay);
    return()=>window.clearTimeout(timer);
  },[busy,delay]);
  return busy&&visible;
}

/** Decode replacements off-screen; callers commit pixels and metadata together.
 * No persistent image cache: the browser manages decoded-image eviction.
 */
export async function prepareImages(urls:string[], signal?:AbortSignal) {
  await Promise.all([...new Set(urls.filter(Boolean))].map(src=>new Promise<void>((resolve,reject)=>{
    if(signal?.aborted){reject(new DOMException('Cancelled','AbortError'));return;}
    const image=new Image();
    let settled=false;
    const finish=(error?:Error)=>{
      if(settled)return;settled=true;
      window.clearTimeout(timer);signal?.removeEventListener('abort',abort);
      image.onload=null;image.onerror=null;
      if(error){image.src='';reject(error);}else resolve();
    };
    const abort=()=>finish(new DOMException('Cancelled','AbortError'));
    const timer=window.setTimeout(()=>finish(new Error('Plot image timed out; showing the previous snapshot.')),60000);
    signal?.addEventListener('abort',abort,{once:true});
    image.onerror=()=>finish(new Error('Plot image could not be loaded; showing the previous snapshot.'));
    image.src=src;
    if(image.decode)image.decode().then(()=>finish(),()=>finish(new Error('Plot image could not be decoded; showing the previous snapshot.')));
    else if(image.complete&&image.naturalWidth)finish();
    else image.onload=()=>finish();
  })));
}

const noImages=()=>[] as string[];
/** Keep the last complete view, including its labels, while a replacement loads.
 * The candidate and image selector must have stable identity between renders.
 */
export function usePreparedView<T>(candidate:T|null, images:(value:T)=>string[]=noImages) {
  const [shown,setShown]=useState<T|null>(null);
  const [failure,setFailure]=useState<{candidate:T;message:string}|null>(null);
  useEffect(()=>{
    if(!candidate)return;
    const controller=new AbortController();
    const urls=images(candidate);
    const commit=()=>{if(!controller.signal.aborted){setShown(candidate);setFailure(null);}};
    if(!urls.length)commit();
    else prepareImages(urls,controller.signal).then(commit).catch(error=>{
      if(!controller.signal.aborted)setFailure({candidate,message:error.message});
    });
    return()=>controller.abort();
  },[candidate,images]);
  const error=failure?.candidate===candidate?failure.message:'';
  return {shown,error,preparing:!!candidate&&candidate!==shown&&!error};
}
