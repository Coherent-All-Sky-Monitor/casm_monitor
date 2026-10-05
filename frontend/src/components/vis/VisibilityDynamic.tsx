import {memo,useLayoutEffect,useRef,useState} from 'react';
import {axisLabel,clock,number,Quantity} from './ArrayPlots';
import {frequencyLabel,niceTicks,timeTicks} from './PlotTicks';
import {chunkValue,MovieChunk,MovieManifest} from './visibilityPlaybackModel';
import {PowerScale,powerLabel,powerValue} from '../powerScale';

const rasters=new WeakMap<MovieChunk,Map<string,HTMLCanvasElement>>();
function raster(c:MovieChunk,m:MovieManifest,pair:number,q:Quantity,conjugate:boolean,powerScale?:PowerScale){
  let cached=rasters.get(c);if(!cached){cached=new Map();rasters.set(c,cached);}
  const key=`${pair}/${q}/${conjugate}/${powerScale??''}`;const found=cached.get(key);if(found)return found;
  const canvas=document.createElement('canvas');canvas.width=c.count;canvas.height=c.channels;
  const ctx=canvas.getContext('2d')!,pixels=ctx.createImageData(c.count,c.channels);
  const [lo,hi]=m.limits[pair].dynamic[powerScale?(powerScale==='db'?'power_db':'power'):q],log=q==='amp'&&!powerScale,a=log?Math.log10(lo):lo,b=log?Math.log10(hi):hi;
  const palette=m.palettes[q==='real'||q==='imag'?'signed':q];
  for(let t=0;t<c.count;t++)for(let ch=0;ch<c.channels;ch++){
    let value=chunkValue(c,c.base+t,pair,ch,powerScale?'real':q);
    if(powerScale)value=powerValue(value,powerScale);
    if(value!==null&&conjugate&&(q==='imag'||q==='phase'))value=-value;
    const rgb=value===null||!Number.isFinite(value)?[29,35,43]:palette[Math.round(Math.max(0,Math.min(1,((log?Math.log10(Math.max(value,lo)):value)-a)/(b-a)))*255)];
    const k=(ch*c.count+t)*4;pixels.data.set([...rgb,255],k);
  }
  ctx.putImageData(pixels,0,0);cached.set(key,canvas);
  // Retain at most two quantities/directions per baseline in each chunk.
  while(cached.size>c.pairs*2)cached.delete(cached.keys().next().value!);
  return canvas;
}

/** Cached colour strips are cropped/blitted; no PNG encode/decode per frame. */
export const VisibilityDynamic=memo(function VisibilityDynamic({manifest:m,chunks,index,pair,quantity:q,zone,mini=false,conjugate=false,powerScale}:{
  manifest:MovieManifest;chunks:MovieChunk[];index:number;pair:number;quantity:Quantity;zone:string;mini?:boolean;conjugate?:boolean;powerScale?:PowerScale;
}){
  const ref=useRef<HTMLCanvasElement>(null),frame=useRef<HTMLDivElement>(null);
  const [size,setSize]=useState({width:640,height:360,ratio:1});
  useLayoutEffect(()=>{
    const measure=()=>{
      const el=frame.current!;const width=Math.round(el.clientWidth),height=Math.round(el.clientHeight);
      if(width&&height)setSize(old=>old.width===width&&old.height===height?old:{width,height,ratio:Math.min(2,window.devicePixelRatio||1)});
    };
    measure();const observer=new ResizeObserver(measure);observer.observe(frame.current!);return()=>observer.disconnect();
  },[]);
  useLayoutEffect(()=>{
    const canvas=ref.current!,ctx=canvas.getContext('2d');if(!ctx)return;
    const w=size.width,h=size.height,large=w>=600&&h>=260,font=large?13:10;
    const l=mini?0:large?76:50,r=mini?0:large?20:12,top=mini?0:12,bottom=mini?0:large?93:64,pw=w-l-r,ph=Math.max(1,h-top-bottom);
    ctx.setTransform(size.ratio,0,0,size.ratio,0,0);
    const end=m.times[index],start=end-m.window_s;
    ctx.fillStyle='white';ctx.fillRect(0,0,w,h);ctx.fillStyle='#1d232b';ctx.fillRect(l,top,pw,ph);
    ctx.save();ctx.beginPath();ctx.rect(l,top,pw,ph);ctx.clip();ctx.imageSmoothingEnabled=false;
    for(const c of chunks){
      const image=raster(c,m,pair,q,conjugate,powerScale);
      // Split each strip at actual gaps. No interpolation or holding across gaps.
      for(let first=0;first<c.count&&c.base+first<=index;){
        let last=first;while(last+1<c.count&&c.base+last+1<=index&&Math.abs(c.times[last+1]-c.times[last]-m.integration_s)<m.integration_s*.05)last++;
        const a=c.times[first]-m.integration_s/2,b=c.times[last]+m.integration_s/2;
        ctx.drawImage(image,first,0,last-first+1,c.channels,l+(a-start)/m.window_s*pw,top,(b-a)/m.window_s*pw,ph);
        first=last+1;
      }
    }
    ctx.restore();if(mini)return;
    ctx.strokeStyle='#4e5967';ctx.lineWidth=1;ctx.strokeRect(l,top,pw,ph);
    ctx.fillStyle='#263247';ctx.font=`${font}px Arial, sans-serif`;ctx.textAlign='right';
    const f0=Math.min(...m.freq_mhz),f1=Math.max(...m.freq_mhz);
    for(const f of niceTicks(f0,f1,Math.max(5,Math.min(10,Math.floor(ph/20)+1)))){
      const y=top+(f1-f)/(f1-f0)*ph;ctx.fillText(frequencyLabel(f),l-8,y+4);ctx.beginPath();ctx.moveTo(l-4,y);ctx.lineTo(l,y);ctx.stroke();
    }
    ctx.save();ctx.translate(large?17:12,top+ph/2);ctx.rotate(-Math.PI/2);ctx.textAlign='center';ctx.fillText('Frequency (MHz)',0,0);ctx.restore();
    ctx.textAlign='center';for(const t of timeTicks(start,end,pw,zone)){
      const x=l+(t-start)/m.window_s*pw;ctx.fillText(clock(t,zone),x,top+ph+18);ctx.beginPath();ctx.moveTo(x,top+ph);ctx.lineTo(x,top+ph+4);ctx.stroke();
    }
    ctx.fillText(`Time (${zone==='UTC'?'UTC':'OVRO local'})`,l+pw/2,top+ph+(large?37:31));
    ctx.textAlign='left';ctx.fillText(powerScale?powerLabel(powerScale):axisLabel(q)+(q==='amp'?' · log':''),l,top+ph+(large?61:49));
    const palette=m.palettes[q==='real'||q==='imag'?'signed':q],bar=Math.min(175,pw*.43),x=l+pw-bar,y=top+ph+(large?48:37);
    palette.forEach((rgb,k)=>{ctx.fillStyle=`rgb(${rgb.join(',')})`;ctx.fillRect(x+k*bar/256,y,bar/256+1,large?9:7);});
    ctx.fillStyle='#263247';const [lo,hi]=m.limits[pair].dynamic[powerScale?(powerScale==='db'?'power_db':'power'):q];ctx.font=`${large?12:9}px Arial, sans-serif`;
    ctx.textAlign='left';ctx.fillText(q==='phase'?'−π':number(lo),x,y+(large?26:20));
    ctx.textAlign='center';ctx.fillText(q==='phase'?'0':number(q==='amp'&&!powerScale?Math.sqrt(lo*hi):(lo+hi)/2),x+bar/2,y+(large?26:20));
    ctx.textAlign='right';ctx.fillText(q==='phase'?'π':number(hi),x+bar,y+(large?26:20));
  },[m,chunks,index,pair,q,zone,mini,conjugate,size,powerScale]);
  return <div ref={frame} className={`vis-movie-dynamic-frame${mini?' mini':''}`}><canvas ref={ref} width={Math.round(size.width*size.ratio)} height={Math.round(size.height*size.ratio)} className="vis-movie-dynamic" role="img"
    data-frame={index} data-pair={pair} data-power-scale={powerScale} aria-label={`${powerScale?powerLabel(powerScale):axisLabel(q)} dynamic spectrum; time ${zone==='UTC'?'UTC':'OVRO local'}, frequency MHz`}/></div>;
});
