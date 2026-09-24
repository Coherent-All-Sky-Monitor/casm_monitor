import { useMemo, useState } from "react";

export type Quantity = "amp" | "real" | "imag" | "phase";
export type Antenna = {packet_idx:number; antenna:number; station:string; position_enu_m:number[]};
export type Tile = {src:string;min:number;max:number;log:boolean;width:number;height:number};
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

export function Spectrum({values,freq,quantity,log=false,range}:{values:(number|null)[];freq:number[];quantity:Quantity;log?:boolean;range?:[number,number]}) {
  const [hover,setHover]=useState<number|null>(null);
  const chart=useMemo(()=>{
    const transform=(v:number|null)=>v==null||!Number.isFinite(v)||(log&&v<=0)?null:log?Math.log10(v):v;
    const data=values.map(transform);const finite=data.filter((v):v is number=>v!==null);
    let lo=range?transform(range[0])??0:Math.min(...finite),hi=range?transform(range[1])??1:Math.max(...finite);
    if(quantity==='phase'){lo=-Math.PI;hi=Math.PI;}else if(!Number.isFinite(lo)||!Number.isFinite(hi)){lo=0;hi=1;}else if(lo===hi){const pad=Math.max(Math.abs(lo)*.05,1);lo-=pad;hi+=pad;}
    const f0=Math.min(...freq),f1=Math.max(...freq);let path="",active=false,previous:number|null=null;
    data.forEach((v,i)=>{if(v===null){active=false;previous=null;return;}const x=39+(freq[i]-f0)/(f1-f0)*182,y=12+(hi-v)/(hi-lo)*90;const wrap=quantity==='phase'&&previous!==null&&Math.abs(v-previous)>Math.PI;path+=`${active&&!wrap?'L':'M'}${x.toFixed(2)},${y.toFixed(2)} `;active=true;previous=v;});
    return {lo,hi,path,f0,f1,label:(v:number)=>quantity==='phase'?(v<0?'−π':'π'):number(log?10**v:v)};
  },[values,freq,quantity,log,range]);
  return <div className="array-spectrum"><svg viewBox="0 0 232 130" role="img" aria-label={`${quantity} versus frequency`} onMouseLeave={()=>setHover(null)} onMouseMove={e=>{const r=e.currentTarget.getBoundingClientRect();const f=chart.f0+(((e.clientX-r.left)/r.width*232)-39)/182*(chart.f1-chart.f0);let k=0;freq.forEach((v,i)=>{if(Math.abs(v-f)<Math.abs(freq[k]-f))k=i;});setHover(k);}}><line x1="39" y1="12" x2="39" y2="102"/><line x1="39" y1="102" x2="221" y2="102"/><line className="spectrum-guide" x1="39" y1="57" x2="221" y2="57"/><text x="34" y="16" textAnchor="end">{chart.label(chart.hi)}</text><text x="34" y="102" textAnchor="end">{chart.label(chart.lo)}</text><text x="39" y="119">{chart.f0.toFixed(0)}</text><text x="221" y="119" textAnchor="end">{chart.f1.toFixed(0)}</text><text x="130" y="126" textAnchor="middle">MHz</text><path d={chart.path}/></svg><span className="spectrum-readout">{hover!==null?`${freq[hover].toFixed(2)} MHz · ${number(values[hover])}${quantity==='phase'?' rad':''}`:quantity==='phase'?'radians':log?'counts · logarithmic axis':'counts'}</span></div>;
}

export function DynamicSpectrum({tile,data,zone,quantity}:{tile:Tile;data:Snapshot;zone:string;quantity:Quantity}) {
  return <div className="array-dynamic"><div className="dynamic-frequency"><span>{Math.max(...data.freq_mhz).toFixed(0)}</span><span>MHz</span><span>{Math.min(...data.freq_mhz).toFixed(0)}</span></div><img src={tile.src} alt={`${quantity} dynamic spectrum`} draggable={false}/><div className="dynamic-time"><span>{clock(data.t0,zone,data.t1-data.t0>20*3600)}</span><span>{clock(data.t1,zone,data.t1-data.t0>20*3600)}</span></div><div className="tile-scale"><i className={`scale-${quantity}`}/><span>{quantity==='phase'?'−π … π rad':`${number(tile.min)} … ${number(tile.max)}${tile.log?' · log':''}`}</span></div></div>;
}

export function PairMatrix({data,selected,quantity,onSelect}:{data:Snapshot;selected:number[];quantity:Quantity;onSelect:(i:number,j:number)=>void}) {
  const ants=data.inputs.filter(a=>selected.includes(a.packet_idx)).sort((a,b)=>location(b).row-location(a).row||location(a).col-location(b).col);
  const ranks=ants.map(a=>data.inputs.indexOf(a));const n=ants.length;
  const values=ranks.flatMap((r,i)=>ranks.flatMap((c,j)=>i!==j&&data.matrix[quantity][r][c]!=null?[data.matrix[quantity][r][c] as number]:[]));
  const max=Math.max(...values.map(Math.abs),1e-20),positive=values.filter(v=>v>0),lo=positive.length?Math.min(...positive):1,hi=Math.max(...positive,lo*1.01);
  const color=(v:number|null)=>v===null?'#1d232b':quantity==='phase'?`hsl(${(v+Math.PI)/(2*Math.PI)*360},60%,55%)`:quantity==='amp'?`hsl(${48-45*Math.min(1,Math.max(0,(Math.log10(Math.max(v,lo))-Math.log10(lo))/(Math.log10(hi)-Math.log10(lo))))},80%,${12+65*Math.min(1,Math.max(0,(Math.log10(Math.max(v,lo))-Math.log10(lo))/(Math.log10(hi)-Math.log10(lo))))}%)`:`hsl(${v<0?8:205},60%,${12+60*Math.min(1,Math.abs(v)/max)}%)`;
  if(!n)return null;
  return <div className="array-matrix"><p>All selected pairs · latest integration · selected frequency band. Click a cell to inspect its baseline.</p><div className="array-matrix-scroll"><table><thead><tr><th>V(row, column)</th>{ants.map(a=><th key={a.packet_idx}>{a.station}<small>{a.antenna}</small></th>)}</tr></thead><tbody>{ants.map((a,i)=><tr key={a.packet_idx}><th>{a.station}<small>ant {a.antenna}</small></th>{ants.map((b,j)=>{const v=data.matrix[quantity][ranks[i]][ranks[j]];return <td key={b.packet_idx}><button className={i===j?'matrix-auto':''} style={{background:color(v)}} title={`${a.station} × ${b.station}: ${number(v)}${quantity==='phase'?' rad':''}`} aria-label={`${a.station} × ${b.station}`} onClick={()=>onSelect(a.packet_idx,b.packet_idx)}>{i===j?'A':''}</button></td>;})}</tr>)}</tbody></table></div><p className="muted">A = autocorrelation. {quantity==='amp'?'Colour: mean |V|, logarithmic; range from cross-correlations.':quantity==='phase'?'Colour: phase of the band-averaged complex visibility; cyclic −π to π.':'Colour: real or imaginary part of the band-averaged complex visibility; symmetric about zero.'}</p></div>;
}
