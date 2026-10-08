import { useEffect, useMemo, useState } from "react";
import { Antenna, axisLabel, clock, location, Quantity, Snapshot, Tile, wiringLabel } from "./ArrayPlots";
import {prepareImages,useDelayedBusy} from '../RefreshState';

export type PairTile = {pair:number[];stored_pair:number[];tile:Tile;valid_fraction:number};
type Batch = {panels:PairTile[];t0:number;t1:number;freq_mhz:number[];quantity:Quantity};
const windows=new Map<string,Map<string,PairTile>>();
const pairKey=(i:number,j:number)=>`${i}:${j}`;

type MatrixView={query:string;tiles:Map<string,PairTile>;data:Snapshot;quantity:Quantity;ants:Antenna[];complete:boolean};
export function PairMatrix({data,selected,quantity,zone='America/Los_Angeles',onSelect}:{data:Snapshot;selected:number[];quantity:Quantity;zone?:string;onSelect:(a:Antenna,b:Antenna,p:PairTile,data:Snapshot,quantity:Quantity)=>void}) {
  const ants=data.inputs.filter(a=>selected.includes(a.packet_idx)).sort((a,b)=>location(b).row-location(a).row||location(a).col-location(b).col);
  const pairs=ants.flatMap((a,i)=>ants.slice(i).map(b=>pairKey(a.packet_idx,b.packet_idx)));
  const query=new URLSearchParams({t0:String(data.t0),t1:String(data.t1),fmin:String(Math.min(...data.freq_mhz)),fmax:String(Math.max(...data.freq_mhz)),reference:data.reference,quantity}).toString();
  const requested=pairs.join(',');
  const [state,setState]=useState<MatrixView|null>(null);
  const [progress,setProgress]=useState({query:'',count:0,total:0});
  const [error,setError]=useState(''),[attempt,setAttempt]=useState(0),[size,setSize]=useState('compact');
  const seeded=useMemo(()=>{
    const found=new Map<string,PairTile>();
    for(const p of data.panels){
      if(!p.images[quantity])continue;
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
    setError('');
    const publish=()=>{
      if(!active)return;
      const count=pairs.filter(k=>cached.has(k)).length,complete=count===pairs.length;
      setProgress({query,count,total:pairs.length});
      // A complete old matrix remains usable until its complete replacement is ready.
      setState(previous=>previous?.complete&&!complete?previous:{query,tiles:new Map(cached),data,quantity,ants,complete});
    };
    setProgress({query,count:0,total:pairs.length});
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
          await prepareImages(d.panels.flatMap(p=>[p.tile.src,p.tile.scale]),controller.signal);
          if(!active)return;
          d.panels.forEach(p=>cached.set(pairKey(...p.pair as [number,number]),p));
          publish();
        }catch(e){if(active)setError((e as Error).message);return;}
      }
    };
    void prepareImages([...cached.values()].flatMap(p=>[p.tile.src,p.tile.scale]),controller.signal).then(()=>{if(active){publish();void work();void work();}}).catch(e=>{if(active)setError(e.message);});
    return()=>{active=false;controller.abort();};
  },[query,requested,attempt,seeded,data.t0,data.t1,quantity]);
  const pending=!error&&(progress.query!==query||progress.count<progress.total);
  const updating=useDelayedBusy(pending);
  const tiles=state?.tiles??new Map<string,PairTile>(),shownAnts=state?.ants??ants,shownQuantity=state?.quantity??quantity;
  const count=shownAnts.flatMap((a,i)=>shownAnts.slice(i).map(b=>pairKey(a.packet_idx,b.packet_idx))).filter(k=>tiles.has(k)).length;
  if(!shownAnts.length)return null;
  return <div className={`array-matrix pair-dynamics matrix-${size}`}>
    <div className="matrix-heading"><p>Upper triangle · V(row, column) · {shownAnts.length*(shownAnts.length-1)/2} cross-pairs + {shownAnts.length} autos on the diagonal</p>
      <label>Thumbnail size<select value={size} onChange={e=>setSize(e.target.value)}><option value="compact">Compact</option><option value="large">Larger</option></select></label></div>
    <p>Each image: time →, frequency ↑, same rolling window and band. Click to enlarge with axes and colour scale. {axisLabel(shownQuantity)}{shownQuantity==='phase'?' · fixed −π to π.':' · per-baseline colour limits; compare shapes, not absolute brightness.'}</p>
    {state&&<p className="matrix-window">{state.data.reference==='raw'?'Raw':'Sun fringe-stopped'} · {clock(state.data.t0,zone,true)}–{clock(state.data.t1,zone,true)} · {zone==='UTC'?'UTC':'OVRO local'}</p>}
    <p className="matrix-status" role="status">{count} / {shownAnts.length*(shownAnts.length+1)/2} dynamic spectra ready{updating?' · Updating…':''}{error&&<> · {error} <button onClick={()=>setAttempt(v=>v+1)}>Retry missing tiles</button></>}</p>
    <div className="array-matrix-scroll" tabIndex={0} aria-label="Upper-triangle dynamic spectra. Scroll to see all columns.">
      <table><thead><tr><th>V(row, column)</th>{shownAnts.map(a=><th scope="col" key={a.packet_idx} title={wiringLabel(a)}>{a.station}<small>ant {a.antenna}</small></th>)}</tr></thead>
        <tbody>{shownAnts.map((a,i)=><tr key={a.packet_idx}><th scope="row" title={wiringLabel(a)}>{a.station}<small>ant {a.antenna}</small></th>
          {shownAnts.map((b,j)=>{
            if(j<i)return <td className="matrix-unused" key={b.packet_idx} aria-hidden="true"/>;
            const p=tiles.get(pairKey(a.packet_idx,b.packet_idx));
            const name=`${a.station} × ${b.station}`;
            return <td key={b.packet_idx}><button disabled={!p} className={i===j?'matrix-auto':''} aria-label={`Expand ${name}`} title={`${a.station}: ${wiringLabel(a)}\n${b.station}: ${wiringLabel(b)}${i===j?' (auto)':''}`} onClick={()=>p&&state&&onSelect(a,b,p,state.data,state.quantity)}>
              {p?<img src={p.tile.src} alt={`${name} ${axisLabel(shownQuantity)} dynamic spectrum`} draggable={false}/>:<span>Loading…</span>}{i===j&&<small>auto</small>}
            </button></td>;
          })}</tr>)}</tbody>
      </table>
    </div>
    <p>Blank lower triangle is intentionally omitted, not missing data. Dark pixels inside images are missing or undefined samples. Grey loading cells are not observations.</p>
  </div>;
}
