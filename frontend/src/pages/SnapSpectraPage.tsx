import { useEffect, useMemo, useState } from 'react';
import { api, Json, Notice, stamp, useResource } from '../components/Workspace';
import { FigureZoom } from '../components/FigureZoom';
import { SnapPlot, SnapTrend } from '../components/SnapPlot';
import '../snap-spectra.css';

type Input = {key:string;ip:string;snap:number;slot:string;adc:number;station:string|null;antenna:number|null;functional:boolean;packet_idx:number|null;board:Json};
const location=(i:Input)=>{const m=/N(\d+)E(\d+)/.exec(i.station??'');return {row:m?Number(m[1]):-1,col:m?Number(m[2]):-1};};

function Expanded({input,frame,reference,days,range,onClose}:{input:Input;frame:Json;reference:Json|null;days:number;range:[number,number];onClose:()=>void}) {
  const {data:loadedTrend,error}=useResource(`/api/snap-workspace/spectra-trend?ip=${encodeURIComponent(input.ip)}&adc=${input.adc}&days=${days}`);
  const data=loadedTrend?.days===days&&loadedTrend?.ip===input.ip&&loadedTrend?.adc===input.adc?loadedTrend:null;
  const [focus,setFocus]=useState(true);
  const powers:number[]=(data?.points??[]).map((p:Json)=>p.power_db).filter((v:number|null)=>v!==null);
  const trendRange:[number,number]=focus&&powers.length?[Math.floor(Math.min(...powers)/5)*5-2.5,Math.ceil(Math.max(...powers)/5)*5+2.5]:range;
  const base=reference?.boards.find((b:Json)=>b.ip===input.ip);
  return <FigureZoom title={`${input.station??'Unwired input'} · SNAP ${input.snap} SLOT ${input.slot} ADC ${input.adc}`} onClose={onClose}>
    <div className="snap-expanded"><p>{stamp(input.board.ts)} · 4096 native channels</p>
      <SnapPlot freq={frame.freq_mhz} values={input.board.spectra_db?.[input.adc]??null} reference={base?.spectra_db?.[input.adc]} range={range}/>
      <h4>Full-band power · past {days} days</h4><p>10 log₁₀(mean linear power) across all 4096 channels. Gaps and EQ-epoch changes are not joined.</p>
      <label className="snap-check"><input type="checkbox" checked={focus} onChange={e=>setFocus(e.target.checked)}/>Focus trend scale on drift (absolute dB)</label>
      {error?<Notice>{error}</Notice>:data?<><SnapPlot freq={[]} values={null} trend={data as SnapTrend} range={trendRange}/><p>{data.points.length} saved reads · {data.points.filter((p:Json)=>p.power_db===null).length} incomplete / zero-power samples</p></>:<p>Reading saved history…</p>}
    </div>
  </FigureZoom>;
}

export default function SnapSpectraPage() {
  const [mode,setMode]=useState('latest'),[layout,setLayout]=useState('snap'),[allAdcs,setAllAdcs]=useState(false),[days,setDays]=useState(30),[at,setAt]=useState<number|null>(null),[compare,setCompare]=useState(false),[hidden,setHidden]=useState<string[]>([]),[expanded,setExpanded]=useState<string|null>(null);
  const [low,setLow]=useState('-100'),[high,setHigh]=useState('0'),[reading,setReading]=useState(false),[receipt,setReceipt]=useState(''),[error,setError]=useState('');
  const {data:acquisition,error:statusError}=useResource('/api/snap-workspace/acquisition',10000);
  const revision=acquisition?.latest_job?.finished??'';
  const {data:loadedCatalog,error:catalogError}=useResource(`/api/snap-workspace/spectra-catalog?days=${days}&v=${revision}`,60000);
  const catalog=loadedCatalog?.days===days?loadedCatalog:null;
  const snapshots:Json[]=catalog?.snapshots??[];
  const selectedAt=at??snapshots[snapshots.length-1]?.at??null;
  const frameUrl=`/api/snap-workspace/spectra?${mode==='history'&&selectedAt!==null?`at=${selectedAt}&`:''}v=${revision}`;
  const {data:loadedFrame,error:frameError}=useResource(frameUrl,mode==='latest'?60000:0);
  const frame=loadedFrame?.selected_at===(mode==='history'?selectedAt:null)?loadedFrame:null;
  const [reference,setReference]=useState<Json|null>(null),[referenceError,setReferenceError]=useState('');
  const baseline=snapshots[0]?.at;
  useEffect(()=>{let active=true;setReference(null);setReferenceError('');if(compare&&baseline!==undefined)api(`/api/snap-workspace/spectra?at=${baseline}`).then(d=>{if(active)setReference(d);}).catch(e=>{if(active)setReferenceError(e.message);});return()=>{active=false;};},[compare,baseline]);
  const inputs:Input[]=useMemo(()=>(frame?.boards??[]).flatMap((board:Json)=>board.inputs.map((i:Json)=>({...i,key:`${board.ip}/${i.adc}`,ip:board.ip,snap:board.feng_id,slot:board.slot,board}))),[frame]);
  const wired=inputs.filter(i=>i.functional&&i.station);
  const visible=inputs.filter(i=>(allAdcs||i.functional)&&!hidden.includes(i.key));
  const rows=[...new Set(wired.map(i=>location(i).row))].sort((a,b)=>b-a);
  const validRange=low.trim()!==''&&high.trim()!==''&&Number.isFinite(Number(low)+Number(high))&&Number(high)>Number(low);
  const range:[number,number]=validRange?[Number(low),Number(high)]:[-100,0];
  const toggle=(key:string)=>setHidden(h=>h.includes(key)?h.filter(k=>k!==key):[...h,key]);
  const acquire=async()=>{setReading(true);setError('');try{
    const status=await api('/api/snap-workspace/acquisition');
    const ips=(frame?.boards??[]).map((b:Json)=>b.ip);if(!ips.length)throw new Error('No configured antenna boards');
    const result=await api('/api/snap-workspace/acquire',{ips,confirm:true},{'X-CASM-Snap-CSRF':status.csrf_token});
    setReceipt(`Read queued · job ${result.job_id}. Latest spectra update when it finishes.`);
  }catch(e){setError((e as Error).message);}finally{setReading(false);}};
  const card=(input:Input)=>{
    const b=input.board, ref=reference?.boards.find((a:Json)=>a.ip===input.ip);
    return <article className="snap-spectrum-card" key={input.key}>
      <header><div><h4>{input.station??`ADC ${input.adc}`}</h4><span>{input.antenna?`Ant ${input.antenna}`:'No wired antenna'} · SNAP {input.snap} SLOT {input.slot} ADC {input.adc}</span></div><button title="Hide this input" aria-label={`Hide SNAP ${input.snap} ADC ${input.adc}`} onClick={()=>toggle(input.key)}>−</button></header>
      <button className="snap-plot-trigger" aria-label={`Expand SNAP ${input.snap} ADC ${input.adc}`} onClick={()=>setExpanded(input.key)}>
        <SnapPlot freq={frame?.freq_mhz??[]} values={b.spectra_db?.[input.adc]??null} reference={ref?.spectra_db?.[input.adc]} range={range}/>
      </button>
      <footer className={b.ts===null||b.error||mode==='latest'&&b.stale?'snap-warning':''}>{b.ts===null?'No read in this snapshot':`${stamp(b.ts)}${mode==='latest'&&b.stale?' · STALE':''}`}
        {!!b.zero_channels?.[input.adc]&&<span> · {b.zero_channels[input.adc]} zero-power channels</span>}
        {!!b.invalid_channels?.[input.adc]&&<span> · {b.invalid_channels[input.adc]} invalid channels</span>}
        {b.error&&<span> · Saved spectrum unavailable</span>}
        {compare&&reference&&<span> · {ref?.ts?`Reference ${stamp(ref.ts)}`:'No reference for this board'}{ref?.eq_epoch&&b.eq_epoch&&ref.eq_epoch!==b.eq_epoch?' · EQ / FFT epoch differs':''}</span>}
      </footer>
    </article>;
  };
  const expandedInput=inputs.find(i=>i.key===expanded);
  return <div className="workspace-page snap-spectra-page">
    <div className="page-heading"><p className="eyebrow">Full-band hardware monitor</p><h2>SNAPs</h2><p className="muted">375–500 MHz · all 4096 channels · saved spectra and drift history</p></div>
    <div className="snap-controls control-surface"><div className="field-row">
      <div className="choice-row" aria-label="SNAP view">{['latest','history'].map(m=><button key={m} className={mode===m?'active':''} aria-pressed={mode===m} onClick={()=>setMode(m)}>{m==='latest'?'Latest spectra':'History'}</button>)}</div>
      <button className="primary" onClick={acquire} disabled={reading||!frame||!acquisition?.manual_enabled||!!acquisition?.manual_refusal}>{reading?'Queuing read…':'Ping now · get spectra'}</button>
      <span className="muted">Configured interval: {acquisition?acquisition.configured_interval_s/3600:'…'} h · page refresh: 60 s</span>
    </div>
    {acquisition?.due&&<Notice>Read is due or overdue. Saved timestamps below show what was actually acquired.</Notice>}
    <div className="snap-job-status">{acquisition?.latest_job&&<span>Read job {acquisition.latest_job.id}: {acquisition.latest_job.state}</span>}{acquisition?.manual_refusal&&<span>{acquisition.manual_refusal.detail}</span>}{receipt&&<span role="status">{receipt}</span>}</div>
    <p className="muted">Diagnostic readout only. No programming, EQ, gains, re-sync or observing-pipeline changes. Browsing history never contacts a SNAP.</p>
    </div>
    {[error,statusError,catalogError,frameError,referenceError].filter(Boolean).map((e,i)=><Notice key={i}>{e}</Notice>)}
    <div className="snap-arrangement"><div className="snap-toolbar control-surface"><div className="field-row">
      <div className="choice-row" aria-label="Panel order">{[['snap','Compact · SNAP order'],['station','Compact · station order'],['grid','Station grid']].map(([v,label])=><button key={v} aria-pressed={layout===v} className={layout===v?'active':''} onClick={()=>setLayout(v)}>{label}</button>)}</div>
      <label className="snap-check"><input type="checkbox" checked={allAdcs} onChange={e=>setAllAdcs(e.target.checked)}/>All 12 ADCs per SNAP</label>
      <button onClick={()=>setHidden([])}>Show all {allAdcs?'48 ADCs':'stations'}</button>
    </div><div className="field-row">
      <label>Shared min · dB<input aria-label="Shared minimum dB" type="number" step="5" value={low} onChange={e=>setLow(e.target.value)}/></label>
      <label>Shared max · dB<input aria-label="Shared maximum dB" type="number" step="5" value={high} onChange={e=>setHigh(e.target.value)}/></label>
      <button onClick={()=>{const v=inputs.flatMap(i=>(i.board.spectra_db?.[i.adc]??[]).filter((p:number|null)=>p!==null)) as number[];if(v.length){setLow(String(Math.floor(v.reduce((a,b)=>Math.min(a,b),Infinity)/10)*10-5));setHigh(String(Math.ceil(v.reduce((a,b)=>Math.max(a,b),-Infinity)/10)*10+5));}}}>Fit current spectra</button>
      <label>History · days<input aria-label="History days" type="number" min="1" max="365" value={days} onChange={e=>{const d=Number(e.target.value);if(Number.isInteger(d)&&d>=1&&d<=365){setDays(d);setAt(null);}}}/></label>
      <label className="snap-check"><input type="checkbox" checked={compare} disabled={!snapshots.length} onChange={e=>setCompare(e.target.checked)}/>Overlay first saved snapshot</label>
    </div>
    {!validRange&&<Notice>Choose a maximum above the minimum; displaying −100 to 0 dB meanwhile.</Notice>}
    <p className="muted">Same power scale across panels and dates; it stays fixed until you change it. 10 log₁₀(native power), not dBm. <span className="snap-legend-current">Blue: selected read.</span> {compare&&<span className="snap-legend-reference">Orange dashed: first read in the selected {days} days.</span>}</p>
    {mode==='history'&&<div className="snap-history-control">{snapshots.length?<><label>Saved acquisition<select aria-label="Saved acquisition" value={selectedAt??''} onChange={e=>setAt(Number(e.target.value))}>{[...snapshots].reverse().map(s=><option key={s.at} value={s.at}>{stamp(s.at)} · {s.boards.length} board{s.boards.length===1?'':'s'}</option>)}</select></label><input aria-label="Snapshot timeline" type="range" min="0" max={snapshots.length-1} step="1" value={Math.max(0,snapshots.findIndex(s=>s.at===selectedAt))} onChange={e=>setAt(snapshots[Number(e.target.value)].at)}/><span>{snapshots.length} saved snapshots · uneven time intervals; missing hours are not filled</span></>:<Notice>No saved acquisitions in these {days} days.</Notice>}</div>}
    </div>
    <details className="snap-layout-key" open><summary>Array layout · {wired.length} wired stations · {visible.length} panels · click a station to show / hide</summary><div className="snap-station-map">{rows.map(row=><div key={row}><strong>N{String(row).padStart(2,'0')}</strong>{[1,2,3,4,5,6].map(col=>{const i=wired.find(a=>location(a).row===row&&location(a).col===col);return <button key={col} disabled={!i} className={i&&!hidden.includes(i.key)?'shown':''} onClick={()=>i&&toggle(i.key)} title={i?`${i.station} · SNAP ${i.snap} SLOT ${i.slot} ADC ${i.adc}`:'No wired antenna'}>{`E${col}`}<small>{i?`S${i.snap} A${i.adc}`:'—'}</small></button>;})}</div>)}</div><p className="muted">Current wiring; empty positions remain marked. Entirely empty plank rows are compressed.</p></details>
    </div>
    {(frame?.boards??[]).filter((b:Json)=>Object.keys(b.latest_errors??{}).length).map((b:Json)=><details className="snap-board-errors" key={b.ip}><summary>SNAP {b.feng_id} · latest attempt {stamp(b.latest_attempt)} has read errors</summary><pre>{JSON.stringify(b.latest_errors,null,2)}</pre><p>Any visible spectrum is the last saved successful read at its labelled time. No hardware recovery is attempted.</p></details>)}
    {!frame?<Notice>Loading saved full-band spectra…</Notice>:mode==='history'&&!snapshots.length?null:<>
      {layout==='snap'?(frame.boards??[]).map((b:Json)=><section className="snap-panel-group" key={b.ip}><header><h3>SNAP {b.feng_id} <span>SLOT {b.slot}</span></h3><span>{b.ip} · ADC order →</span></header><div className="snap-panel-grid">{visible.filter(i=>i.ip===b.ip).map(card)}</div></section>):rows.map(row=><section className="snap-panel-group" key={row}><header><h3>N{String(row).padStart(2,'0')}</h3><span>West → east · E1–E6</span></header><div className={`snap-panel-grid ${layout==='grid'?'snap-physical-grid':''}`}>{layout==='grid'?[1,2,3,4,5,6].map(col=>{const i=visible.find(a=>location(a).row===row&&location(a).col===col);return i?card(i):<div className="snap-empty" key={col}>E{col} · {wired.some(a=>location(a).row===row&&location(a).col===col)?'Hidden':'No antenna'}</div>;}):visible.filter(i=>location(i).row===row).sort((a,b)=>location(a).col-location(b).col).map(card)}</div></section>)}
      {layout!=='snap'&&allAdcs&&<section className="snap-panel-group"><header><h3>Unwired ADCs · SNAP order</h3></header><div className="snap-panel-grid">{visible.filter(i=>!i.functional||!i.station).map(card)}</div></section>}
    </>}
    {expandedInput&&frame&&<Expanded input={expandedInput} frame={frame} reference={reference} days={days} range={range} onClose={()=>setExpanded(null)}/>}
  </div>;
}
