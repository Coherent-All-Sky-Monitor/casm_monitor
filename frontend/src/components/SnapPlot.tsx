import { useEffect, useId, useRef, useState } from 'react';
import { niceTicks } from './vis/PlotTicks';

type Value = number | null;
export type SnapTrend = {start:number; end:number; gap_s:number; points:{ts:number;power_db:Value;eq_epoch:string|null;zero_channels:number|null;error:string|null}[]};

/** Native-channel trace: no rebinning or normalization. Missing values break it. */
export function SnapPlot({freq, values, reference, range, trend}:{freq:number[];values:Value[]|null;reference?:Value[]|null;range:[number,number];trend?:SnapTrend}) {
  const host=useRef<HTMLDivElement>(null), clip=useId().replace(/:/g,'');
  const [width,setWidth]=useState(350),[hover,setHover]=useState('');
  useEffect(()=>{const o=new ResizeObserver(([e])=>setWidth(Math.max(250,e.contentRect.width)));if(host.current)o.observe(host.current);return()=>o.disconnect();},[]);
  const [lo,hi]=range, left=57,right=width-16,top=16,bottom=171,height=219;
  const t0=trend?.start??375,t1=trend?.end??500;
  const x=(v:number)=>left+(v-t0)/(t1-t0)*(right-left), y=(v:number)=>top+(hi-v)/(hi-lo)*(bottom-top);
  const trace=(axis:number[],line:Value[],epochs?:(string|null)[])=>{
    let d='',last:number|null=null;
    line.forEach((v,i)=>{
      if(v===null||!Number.isFinite(v)){last=null;return;}
      const joined=last!==null&&(!trend||(axis[i]-axis[last]<=trend.gap_s&&epochs?.[i]===epochs?.[last]));
      d+=`${joined?'L':'M'}${x(axis[i]).toFixed(2)},${y(v).toFixed(2)} `;last=i;
    });return d;
  };
  const axis=trend?trend.points.map(p=>p.ts):freq, line=trend?trend.points.map(p=>p.power_db):values??[];
  const finite=line.filter((v):v is number=>v!==null&&Number.isFinite(v));
  const clipped=finite.filter(v=>v<lo||v>hi).length;
  const tickCount=Math.max(2,Math.min(6,Math.floor((right-left)/110)));
  const ticks=trend?Array.from({length:tickCount},(_,i)=>t0+(t1-t0)*i/(tickCount-1)):niceTicks(375,500,Math.min(14,(right-left)/42));
  const date=(t:number)=>new Date(t*1000).toISOString().slice(5,16).replace('T',' ');
  return <div className="snap-plot" ref={host}>
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={trend?'Full-band power (dB) versus time (UTC)':'Power (dB) versus frequency (MHz), 4096 channels'}
      onMouseLeave={()=>setHover('')} onMouseMove={e=>{
        const r=e.currentTarget.getBoundingClientRect(),v=t0+((e.clientX-r.left)/r.width*width-left)/(right-left)*(t1-t0);
        let k=0;axis.forEach((a,i)=>{if(Math.abs(a-v)<Math.abs(axis[k]-v))k=i;});
        setHover(axis.length?`${trend?date(axis[k])+' UTC':axis[k].toFixed(3)+' MHz'} · ${line[k]==null?'no positive power':line[k]!.toFixed(2)+' dB'}`:'');
      }}>
      <defs><clipPath id={clip}><rect x={left} y={top} width={right-left} height={bottom-top}/></clipPath></defs>
      {niceTicks(lo,hi,6).map(v=><g key={v}><line className="snap-guide" x1={left} x2={right} y1={y(v)} y2={y(v)}/><text x={left-7} y={y(v)+4} textAnchor="end">{v}</text></g>)}
      {ticks.map((v,i)=><g key={v}><line className="snap-guide" x1={x(v)} x2={x(v)} y1={top} y2={bottom}/><text x={x(v)} y={bottom+18} textAnchor={trend&&i===0?'start':trend&&i===ticks.length-1?'end':'middle'}>{trend?date(v):v}</text></g>)}
      <path className="snap-axis" d={`M${left},${top}V${bottom}H${right}`}/>
      <text x={(left+right)/2} y={height-7} textAnchor="middle">{trend?'Time (UTC)':'Frequency (MHz)'}</text>
      <text transform={`translate(14 ${(top+bottom)/2}) rotate(-90)`} textAnchor="middle">Power (dB)</text>
      <g clipPath={`url(#${clip})`}>
        {!trend&&reference&&<path className="snap-reference" d={trace(freq,reference)}/>}
        <path className="snap-trace" d={trace(axis,line,trend?.points.map(p=>p.eq_epoch))}/>
        {trend&&trend.points.map(p=>p.power_db===null?null:<circle key={p.ts} cx={x(p.ts)} cy={y(p.power_db)} r="3" fill="#194fa3"><title>{date(p.ts)} UTC · {p.power_db} dB · EQ {p.eq_epoch??'unknown'}</title></circle>)}
      </g>
      {!finite.length&&<text x={(left+right)/2} y="93" textAnchor="middle">{values===null&&!trend?'No saved spectrum':'No positive power samples'}</text>}
    </svg>
    <div className="snap-readout">{hover|| (clipped?`${clipped} points outside scale — adjust dB limits`:'\u00a0')}</div>
  </div>;
}
