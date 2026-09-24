import { useEffect, useMemo, useRef, useState } from "react";
import { frequencyLabel, niceTicks, timeTicks } from "./PlotTicks";

export type Quantity = "amp" | "real" | "imag" | "phase";
export type Antenna = {packet_idx:number; antenna:number; station:string; position_enu_m:number[]; snap?:number|null; slot?:string|null; adc?:number|null};
export function wiringLabel(a:Antenna) {return `SNAP ${a.snap??'—'} · SLOT ${a.slot??'—'} · ADC ${a.adc??'—'}`;}
export type Tile = {src:string;scale:string;min:number;max:number;log:boolean;width:number;height:number};
export type Panel = {input:number;pair:number[];is_auto:boolean;valid_fraction:number;spectra:Record<Quantity,{latest:(number|null)[];mean:(number|null)[]}>;images:Record<Quantity,Tile>};
export type Snapshot = {inputs:Antenna[];default_inputs:number[];reference_input:number;reference:string;mode:string;panels:Panel[];freq_mhz:number[];t0:number;t1:number;samples:number;integration_s:number;matrix:Record<Quantity,(number|null)[][]>;provenance:Record<string,unknown>};
export const QUANTITIES: [Quantity,string][] = [["amp","|V|"],["real","Real(V)"],["imag","Imag(V)"],["phase","Phase"]];
export function number(v:number|null|undefined) {if(v==null||!Number.isFinite(v))return "—";if(v===0)return "0";return Math.abs(v)>=1e4||Math.abs(v)<.01?v.toExponential(1):Number(v.toPrecision(3)).toString();}
export function clock(t:number,zone:string,dated=false) {return new Intl.DateTimeFormat("en-US",{...(dated?{month:"short",day:"numeric"} as const:{}),timeZone:zone,hour:"2-digit",minute:"2-digit",hour12:false}).format(new Date(t*1000));}
export function location(a:Antenna) {const m=/N(\d+)E(\d+)/.exec(a.station??"");return {row:m?Number(m[1]):0,col:m?Number(m[2]):1};}

export function LayoutMap({inputs,selected,reference,mode,onToggle}:{inputs:Antenna[];selected:number[];reference:number;mode:string;onToggle:(p:number)=>void}) {
  const rows=[...new Set(inputs.map(a=>location(a).row))].sort((a,b)=>b-a);
  return <div className="array-map" aria-label="Antenna layout. North is up; east is right."><div className="array-map-heading"><span>North ↑</span><span>East →</span></div><div className="array-map-grid"><span/>{[1,2,3,4,5,6].map(c=><span className="map-col" key={c}>E{c}</span>)}{rows.map(row=><div className="array-map-row" key={row}><span className="map-row">N{String(row).padStart(2,"0")}</span>{[1,2,3,4,5,6].map(col=>{const a=inputs.find(i=>location(i).row===row&&location(i).col===col);return a?<button type="button" key={col} aria-pressed={selected.includes(a.packet_idx)} aria-label={`${a.station}, antenna ${a.antenna}`} title={`${a.station} · ant ${a.antenna}${mode==='cross'&&a.packet_idx===reference?' · reference':''}`} className={`map-antenna ${selected.includes(a.packet_idx)?'selected':''} ${mode==='cross'&&a.packet_idx===reference?'reference':''}`} onClick={()=>onToggle(a.packet_idx)}>{a.antenna}</button>:<span key={col} className="map-empty" title={`N${String(row).padStart(2,"0")}E${col}: no wired antenna`} aria-label={`N${String(row).padStart(2,"0")}E${col}: no wired antenna`}>×</span>;})}</div>)}</div><p>North-up station map · × no wired antenna<br/>Click an antenna to include or hide it.</p></div>;
}

export const quantityLabel = (q:Quantity) => QUANTITIES.find(([key])=>key===q)![1];
export const quantityUnits = (q:Quantity) => q==='phase'?'rad':'counts';
export const axisLabel = (q:Quantity) => `${quantityLabel(q)} (${quantityUnits(q)})`;

export function Spectrum({values,freq,quantity,log=false,range}:{values:(number|null)[];freq:number[];quantity:Quantity;log?:boolean;range?:[number,number]}) {
  const [hover,setHover]=useState<number|null>(null);
  const chart=useMemo(()=>{
    const transform=(v:number|null)=>v==null||!Number.isFinite(v)||(log&&v<=0)?null:log?Math.log10(v):v;
    const data=values.map(transform),finite=data.filter((v):v is number=>v!==null);
    let lo=range?transform(range[0])??0:Math.min(...finite),hi=range?transform(range[1])??1:Math.max(...finite);
    if(quantity==='phase'){lo=-Math.PI;hi=Math.PI;}
    else if(!Number.isFinite(lo)||!Number.isFinite(hi)){lo=0;hi=1;}
    else if(lo===hi){const pad=Math.max(Math.abs(lo)*.05,1);lo-=pad;hi+=pad;}
    const f0=Math.min(...freq),f1=Math.max(...freq);let path="",active=false,previous:number|null=null;
    data.forEach((v,i)=>{
      if(v===null){active=false;previous=null;return;}
      const x=64+(freq[i]-f0)/(f1-f0)*244,y=12+(hi-v)/(hi-lo)*137;
      const wrap=quantity==='phase'&&previous!==null&&Math.abs(v-previous)>Math.PI;
      path+=`${active&&!wrap?'L':'M'}${x.toFixed(2)},${y.toFixed(2)} `;
      active=true;previous=v;
    });
    const ticks=quantity==='phase'?[-Math.PI,-Math.PI/2,0,Math.PI/2,Math.PI]:niceTicks(lo,hi,6);
    let frequencyTicks=niceTicks(f0,f1,11);
    // SVG text scales with the figure, so reserve space in viewBox units.
    const longest=Math.max(...frequencyTicks.map(f=>frequencyLabel(f).length));
    const count=Math.floor(244/(longest*5+8))+1;
    if(frequencyTicks.length>count)frequencyTicks=niceTicks(f0,f1,count);
    const label=(v:number)=>quantity==='phase'?(v===0?'0':v===Math.PI?'π':v===-Math.PI?'−π':v<0?'−π/2':'π/2'):number(log?10**v:v);
    return {lo,hi,path,f0,f1,ticks,frequencyTicks,label};
  },[values,freq,quantity,log,range]);
  return <div className="array-spectrum">
    <svg viewBox="0 0 324 192" role="img" aria-label={`${axisLabel(quantity)} versus Frequency (MHz)`}
      onMouseLeave={()=>setHover(null)} onMouseMove={e=>{
        const r=e.currentTarget.getBoundingClientRect();
        const f=chart.f0+(((e.clientX-r.left)/r.width*324)-64)/244*(chart.f1-chart.f0);
        let k=0;freq.forEach((v,i)=>{if(Math.abs(v-f)<Math.abs(freq[k]-f))k=i;});setHover(k);
      }}>
      <line x1="64" y1="12" x2="64" y2="149"/><line x1="64" y1="149" x2="308" y2="149"/>
      {chart.ticks.map(v=>{const y=12+(chart.hi-v)/(chart.hi-chart.lo)*137;return <g key={v}>
        <line className="spectrum-guide" x1="64" y1={y} x2="308" y2={y}/>
        <line x1="61" y1={y} x2="64" y2={y}/>
        <text className="spectrum-y-tick" x="59" y={y+3} textAnchor="end">{chart.label(v)}</text>
      </g>;})}
      {chart.frequencyTicks.map(f=>{const x=64+(f-chart.f0)/(chart.f1-chart.f0)*244;return <g key={f}><line x1={x} y1="149" x2={x} y2="152"/><text className="spectrum-x-tick" x={x} y="163" textAnchor="middle">{frequencyLabel(f)}</text></g>;})}
      <text className="axis-label" x="186" y="183" textAnchor="middle">Frequency (MHz)</text>
      <text className="axis-label" transform="translate(14 80) rotate(-90)" textAnchor="middle">{axisLabel(quantity)}{log?' · log scale':''}</text>
      <path d={chart.path}/>
    </svg>
    <span className="spectrum-readout">{hover!==null?`${freq[hover].toFixed(3)} MHz · ${number(values[hover])} ${quantityUnits(quantity)}`:'Hover for values · click to enlarge'}</span>
  </div>;
}

export type DynamicAxes = Pick<Snapshot,'freq_mhz'|'t0'|'t1'|'integration_s'>;
export function DynamicSpectrum({tile,data,zone,quantity}:{tile:Tile;data:DynamicAxes;zone:string;quantity:Quantity}) {
  const raster=useRef<HTMLImageElement>(null);
  const [size,setSize]=useState({width:300,height:170});
  useEffect(()=>{
    const observer=new ResizeObserver(([entry])=>{
      const width=Math.round(entry.contentRect.width),height=Math.round(entry.contentRect.height);
      setSize(old=>old.width===width&&old.height===height?old:{width,height});
    });
    if(raster.current)observer.observe(raster.current);
    return()=>observer.disconnect();
  },[]);
  const f0=Math.min(...data.freq_mhz),f1=Math.max(...data.freq_mhz);
  const frequencyTicks=niceTicks(f0,f1,Math.min(25,size.height/17+1));
  const frequencyWidth=Math.max(37,...frequencyTicks.map(f=>frequencyLabel(f).length*6+4));
  const times=timeTicks(data.t0,data.t1,size.width,zone);
  return <div className="dynamic-figure">
    <div className="array-dynamic" style={{gridTemplateColumns:`15px ${frequencyWidth}px minmax(0, 1fr)`}}>
      <span className="dynamic-y-label">Frequency (MHz)</span>
      <div className="dynamic-frequency">{frequencyTicks.map(f=>{const p=(f1-f)/(f1-f0),anchor=p<.03?0:p>.97?100:50;return <span key={f} style={{top:`${p*100}%`,transform:`translateY(-${anchor}%)`,['--tick-anchor' as string]:`${anchor}%`}}>{frequencyLabel(f)}</span>;})}</div>
      <img ref={raster} src={tile.src} alt={`${quantityLabel(quantity)} dynamic spectrum`} draggable={false}/>
      <div className="dynamic-time">{times.map(t=>{const p=(t-data.t0)/(data.t1-data.t0),anchor=p<.05?0:p>.95?100:50;return <span key={t} title={clock(t,zone,true)} style={{left:`${p*100}%`,transform:`translateX(-${anchor}%)`,['--tick-anchor' as string]:`${anchor}%`}}>{clock(t,zone)}</span>;})}</div>
      <div className="dynamic-x-label">Time ({zone==='UTC'?'UTC':zone})<small>{clock(data.t0,zone,true)} – {clock(data.t1,zone,true)}</small></div>
    </div>
    <div className="tile-scale" aria-label={`${axisLabel(quantity)} colour scale`}>
      <span>{axisLabel(quantity)}{tile.log?' · log scale':''}</span>
      <div className="tile-scale-bar"><img src={tile.scale} alt="" draggable={false}/><div><span>{quantity==='phase'?'−π':number(tile.min)}</span><span>{quantity==='phase'?'0':number(tile.log?Math.sqrt(tile.min*tile.max):(tile.min+tile.max)/2)}</span><span>{quantity==='phase'?'π':number(tile.max)}</span></div></div>
    </div>
  </div>;
}
