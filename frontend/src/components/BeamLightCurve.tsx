import { useEffect, useRef, useState } from 'react';
import { clock, number } from './vis/ArrayPlots';
import { niceTicks, timeTicks } from './vis/PlotTicks';

export type LightCurve = {time_unix:number[]; cross_power:(number|null)[]; channels:number; freq_range_mhz:number[]};

/** Native-channel mean of the displayed fixed beam; gaps are never joined. */
export function BeamLightCurve({curve,t0,t1,integration,transit,zone}:{curve:LightCurve;t0:number;t1:number;integration:number;transit:number;zone:string}) {
  const container=useRef<HTMLDivElement>(null);
  const [width,setWidth]=useState(600),[hover,setHover]=useState<number|null>(null);
  useEffect(()=>{
    const observer=new ResizeObserver(([entry])=>setWidth(Math.max(280,Math.round(entry.contentRect.width))));
    if(container.current)observer.observe(container.current);
    return()=>observer.disconnect();
  },[]);
  const values=curve.cross_power, times=curve.time_unix;
  const finite=values.filter((v):v is number=>v!==null&&Number.isFinite(v));
  let lo=finite.length?Math.min(...finite):0,hi=finite.length?Math.max(...finite):1;
  const pad=hi===lo?Math.max(Math.abs(lo)*.05,1):(hi-lo)*.08;
  lo-=pad;hi+=pad;
  const left=76,right=width-18,top=22,bottom=152;
  const x=(t:number)=>left+(t-t0)/Math.max(t1-t0,integration)*(right-left);
  const y=(v:number)=>top+(hi-v)/(hi-lo)*(bottom-top);
  let path='',previous:number|null=null;
  const points:{x:number;y:number;t:number;v:number}[]=[];
  values.forEach((v,i)=>{
    if(v===null||!Number.isFinite(v)){previous=null;return;}
    const t=times[i],px=x(t),py=y(v);
    path+=`${previous!==null&&t-previous<=integration*1.5?'L':'M'}${px.toFixed(2)},${py.toFixed(2)} `;
    points.push({x:px,y:py,t,v});previous=t;
  });
  return <div ref={container} className="beam-light-curve">
    <p>Band-averaged cross-power</p>
    <svg viewBox={`0 0 ${width} 202`} role="img" aria-label="Band-averaged beam cross-power (weighted counts) versus time"
      onMouseLeave={()=>setHover(null)} onMouseMove={e=>{
        const rect=e.currentTarget.getBoundingClientRect();
        const t=t0+((e.clientX-rect.left)/rect.width*width-left)/(right-left)*(t1-t0);
        let k=0;times.forEach((v,i)=>{if(Math.abs(v-t)<Math.abs(times[k]-t))k=i;});setHover(k);
      }}>
      {niceTicks(lo,hi,5).map(v=><g key={v}><line className="curve-guide" x1={left} x2={right} y1={y(v)} y2={y(v)}/><text x={left-7} y={y(v)+4} textAnchor="end">{number(v)}</text></g>)}
      <line className="curve-axis" x1={left} x2={left} y1={top} y2={bottom}/>
      <line className="curve-axis" x1={left} x2={right} y1={bottom} y2={bottom}/>
      {timeTicks(t0,t1,right-left,zone).map(t=><g key={t}><line className="curve-axis" x1={x(t)} x2={x(t)} y1={bottom} y2={bottom+4}/><text className="curve-time-tick" x={x(t)} y={bottom+17} textAnchor="middle">{clock(t,zone)}</text></g>)}
      <text x={(left+right)/2} y="193" textAnchor="middle">Time ({zone==='UTC'?'UTC':'OVRO local'})</text>
      <text transform="translate(15 87) rotate(-90)" textAnchor="middle">Power (weighted counts)</text>
      {transit>=t0&&transit<=t1&&<g><line className="curve-transit" x1={x(transit)} x2={x(transit)} y1={top} y2={bottom}/><text x={Math.max(left+22,Math.min(right-22,x(transit)))} y="14" textAnchor="middle">Transit</text></g>}
      <path className="curve-trace" d={path}/>
      {points.map(p=><circle className="curve-point" key={p.t} cx={p.x} cy={p.y} r="1.7"><title>{clock(p.t,zone)} · {number(p.v)} weighted counts</title></circle>)}
      {!finite.length&&<text x={(left+right)/2} y="87" textAnchor="middle">No complete band samples</text>}
    </svg>
    <p className="curve-readout">{hover!==null?`${clock(times[hover],zone)} · ${number(values[hover])} weighted counts`:`${curve.channels} usable native channels · no time smoothing`}</p>
  </div>;
}
