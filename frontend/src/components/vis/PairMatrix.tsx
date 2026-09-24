import { useEffect, useMemo, useState } from "react";
import { Antenna, axisLabel, location, Quantity, Snapshot, Tile, wiringLabel } from "./ArrayPlots";

export type PairTile = {pair:number[];stored_pair:number[];tile:Tile;valid_fraction:number};
type Batch = {panels:PairTile[];t0:number;t1:number;freq_mhz:number[];quantity:Quantity};
const windows=new Map<string,Map<string,PairTile>>();
const pairKey=(i:number,j:number)=>`${i}:${j}`;

export function PairMatrix({data,selected,quantity,onSelect}:{data:Snapshot;selected:number[];quantity:Quantity;onSelect:(a:Antenna,b:Antenna,p:PairTile)=>void}) {
  const ants=data.inputs.filter(a=>selected.includes(a.packet_idx)).sort((a,b)=>location(b).row-location(a).row||location(a).col-location(b).col);
  const pairs=ants.flatMap((a,i)=>ants.slice(i).map(b=>pairKey(a.packet_idx,b.packet_idx)));
  const query=new URLSearchParams({t0:String(data.t0),t1:String(data.t1),fmin:String(Math.min(...data.freq_mhz)),fmax:String(Math.max(...data.freq_mhz)),reference:data.reference,quantity}).toString();
  const requested=pairs.join(',');
  const [state,setState]=useState<{query:string;tiles:Map<string,PairTile>}>({query:'',tiles:new Map()});
  const [error,setError]=useState(''),[attempt,setAttempt]=useState(0),[size,setSize]=useState('compact');
  const seeded=useMemo(()=>{
    const found=new Map<string,PairTile>();
    for(const p of data.panels){
      const i=p.input,j=p.is_auto?p.input:data.reference_input;
      found.set(pairKey(i,j),{pair:[i,j],stored_pair:p.pair,tile:p.images[quantity],valid_fraction:p.valid_fraction});
    }
    return found;
  },[data,quantity]);
  useEffect(()=>{
    const controller=new AbortController();let active=true;
    const cached=windows.get(query)??new Map<string,PairTile>();
    seeded.forEach((p,k)=>cached.set(k,p));windows.set(query,cached);
    while(windows.size>4)windows.delete(windows.keys().next().value!);
    setState({query,tiles:new Map(cached)});setError('');
    const missing=requested.split(',').filter(k=>!cached.has(k));let cursor=0;
    const work=async()=>{
      while(active&&cursor<missing.length){
        const batch=missing.slice(cursor,cursor+16);cursor+=16;
        try{
          const r=await fetch(`/api/science/array/pairs?${query}&pairs=${encodeURIComponent(batch.join(','))}`,{signal:controller.signal});
          const result=await r.json();if(!r.ok)throw new Error(result.detail??`HTTP ${r.status}`);
          if(!active)return;
          const d=result as Batch;
          if(d.t0!==data.t0||d.t1!==data.t1||d.quantity!==quantity)throw new Error('Snapshot changed; refresh the array');
          d.panels.forEach(p=>cached.set(pairKey(...p.pair as [number,number]),p));
          setState({query,tiles:new Map(cached)});
        }catch(e){if(active)setError((e as Error).message);return;}
      }
    };
    void work();void work();
    return()=>{active=false;controller.abort();};
  },[query,requested,attempt,seeded,data.t0,data.t1,quantity]);
  const tiles=state.query===query?state.tiles:seeded;
  const count=pairs.filter(k=>tiles.has(k)).length;
  if(!ants.length)return null;
  return <div className={`array-matrix pair-dynamics matrix-${size}`}>
    <div className="matrix-heading"><p>Upper triangle · V(row, column) · {ants.length*(ants.length-1)/2} cross-pairs + {ants.length} autos on the diagonal</p>
      <label>Thumbnail size<select value={size} onChange={e=>setSize(e.target.value)}><option value="compact">Compact</option><option value="large">Larger</option></select></label></div>
    <p>Each image: time →, frequency ↑, same rolling window and band. Click to enlarge with axes and colour scale. {axisLabel(quantity)}{quantity==='phase'?' · fixed −π to π.':' · per-baseline colour limits; compare shapes, not absolute brightness.'}</p>
    <p className="matrix-status" role="status">{count} / {pairs.length} dynamic spectra ready{count<pairs.length&&!error?' · loading bounded batches…':''}{error&&<> · {error} <button onClick={()=>setAttempt(v=>v+1)}>Retry missing tiles</button></>}</p>
    <div className="array-matrix-scroll" tabIndex={0} aria-label="Upper-triangle dynamic spectra. Scroll to see all columns.">
      <table><thead><tr><th>V(row, column)</th>{ants.map(a=><th scope="col" key={a.packet_idx} title={wiringLabel(a)}>{a.station}<small>ant {a.antenna}</small></th>)}</tr></thead>
        <tbody>{ants.map((a,i)=><tr key={a.packet_idx}><th scope="row" title={wiringLabel(a)}>{a.station}<small>ant {a.antenna}</small></th>
          {ants.map((b,j)=>{
            if(j<i)return <td className="matrix-unused" key={b.packet_idx} aria-hidden="true"/>;
            const p=tiles.get(pairKey(a.packet_idx,b.packet_idx));
            const name=`${a.station} × ${b.station}`;
            return <td key={b.packet_idx}><button disabled={!p} className={i===j?'matrix-auto':''} aria-label={`Expand ${name}`} title={`${a.station}: ${wiringLabel(a)}\n${b.station}: ${wiringLabel(b)}${i===j?' (auto)':''}`} onClick={()=>p&&onSelect(a,b,p)}>
              {p?<img src={p.tile.src} alt={`${name} ${axisLabel(quantity)} dynamic spectrum`} draggable={false}/>:<span>Loading…</span>}{i===j&&<small>auto</small>}
            </button></td>;
          })}</tr>)}</tbody>
      </table>
    </div>
    <p>Blank lower triangle is intentionally omitted, not missing data. Dark pixels inside images are missing or undefined samples. Grey loading cells are not observations.</p>
  </div>;
}
