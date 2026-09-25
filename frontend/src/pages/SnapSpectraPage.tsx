import { useMemo, useState } from 'react';
import { api, Json, Notice, stamp, useResource } from '../components/Workspace';
import { FigureZoom } from '../components/FigureZoom';
import { SnapPlot, SnapTrend } from '../components/SnapPlot';
import { dbOffset, snapRange, SNAP_REFERENCE_POWER } from '../components/snapPower';
import { SnapHealth } from '../components/SnapHealth';
import '../snap-spectra.css';

type Input = {key:string;ip:string;snap:number;slot:string;adc:number;station:string|null;antenna:number|null;functional:boolean;packet_idx:number|null;board:Json;beamforming:boolean|null};
const location=(i:Input)=>{const m=/N(\d+)E(\d+)/.exec(i.station??'');return {row:m?Number(m[1]):-1,col:m?Number(m[2]):-1};};

function Expanded({input,frame,days,range,powerReference,onClose}:{input:Input;frame:Json;days:number;range:[number,number];powerReference:number;onClose:()=>void}) {
  const {data:loadedTrend,error}=useResource(`/api/snap-workspace/spectra-trend?ip=${encodeURIComponent(input.ip)}&adc=${input.adc}&days=${days}`);
  const data=loadedTrend?.days===days&&loadedTrend?.ip===input.ip&&loadedTrend?.adc===input.adc?loadedTrend:null;
  const powers:number[]=(data?.points??[]).map((p:Json)=>p.power_db).filter((v:number|null)=>v!==null).map((v:number)=>v+dbOffset(powerReference));
  const trendRange:[number,number]=powers.length?[Math.floor(Math.min(...powers)/5)*5-2.5,Math.ceil(Math.max(...powers)/5)*5+2.5]:range;
  return <FigureZoom title={`${input.station??'Unwired input'} · SNAP ${input.snap} SLOT ${input.slot} ADC ${input.adc}`} onClose={onClose}>
    <div className="snap-expanded"><p>{stamp(input.board.ts)} · 4096 native channels</p>
      <SnapPlot freq={frame.freq_mhz} values={input.board.spectra_db?.[input.adc]??null} range={range} powerReference={powerReference}/>
      <h4>Full-band power · past {days} days</h4><p>Mean power across all 4096 channels · dB</p>
      {data?.history_start&&<p className="muted">History since {stamp(data.history_start)}</p>}
      {error?<Notice>{error}</Notice>:data?<><SnapPlot freq={[]} values={null} trend={data as SnapTrend} range={trendRange} powerReference={powerReference}/><p>{data.points.length} saved reads · {data.points.filter((p:Json)=>p.power_db===null).length} incomplete / zero-power samples</p></>:<p>Reading saved history…</p>}
    </div>
  </FigureZoom>;
}

export default function SnapSpectraPage() {
  const [layout,setLayout]=useState('snap'),[scope,setScope]=useState('beamforming'),[overrides,setOverrides]=useState<Record<string,boolean>>({}),[expanded,setExpanded]=useState<string|null>(null);
  const days=30,powerReference=SNAP_REFERENCE_POWER;
  const allAdcs=scope==='all';
  const [reading,setReading]=useState(false),[receipt,setReceipt]=useState(''),[error,setError]=useState('');
  const {data:acquisition,error:statusError}=useResource('/api/snap-workspace/acquisition',10000);
  const health=useResource('/api/snap-workspace/health',30000);
  const membership=useResource('/api/snap-workspace/beamforming',30000);
  const membershipKnown=!membership.error&&membership.data?.status==='complete';
  const beamformingKeys=useMemo(()=>new Set<string>(membershipKnown?(membership.data?.inputs??[]).filter((i:Json)=>i.beamforming===true).map((i:Json)=>`${i.ip}/${i.adc}`):[]),[membershipKnown,membership.data]);
  const revision=acquisition?.latest_job?.finished??'';
  const {data:frame,error:frameError}=useResource(`/api/snap-workspace/spectra?v=${revision}`,60000);
  const inputs:Input[]=useMemo(()=>(frame?.boards??[]).flatMap((board:Json)=>board.inputs.map((i:Json)=>({...i,key:`${board.ip}/${i.adc}`,ip:board.ip,snap:board.feng_id,slot:board.slot,board,beamforming:membershipKnown?beamformingKeys.has(`${board.ip}/${i.adc}`):null}))),[frame,membershipKnown,beamformingKeys]);
  const wired=inputs.filter(i=>i.functional&&i.station);
  const isVisible=(i:Input)=>overrides[i.key]??(allAdcs||(scope==='wired'?i.functional:i.beamforming===true));
  const visible=inputs.filter(isVisible);
  const rows=[...new Set(wired.map(i=>location(i).row))].sort((a,b)=>b-a);
  const sharedRange=useMemo(()=>{
    const values:(number|null)[]=[];
    for(const input of visible)values.push(...(input.board.spectra_db?.[input.adc]??[]));
    return snapRange(values,powerReference);
  },[visible,powerReference]);
  const toggle=(key:string)=>{const i=inputs.find(i=>i.key===key);if(i)setOverrides(v=>({...v,[key]:!isVisible(i)}));};
  const selectScope=(value:string)=>{setScope(value);setOverrides({});};
  const acquire=async()=>{setReading(true);setError('');try{
    const status=await api('/api/snap-workspace/acquisition');
    const ips=(frame?.boards??[]).map((b:Json)=>b.ip);if(!ips.length)throw new Error('No configured antenna boards');
    const result=await api('/api/snap-workspace/acquire',{ips,confirm:true},{'X-CASM-Snap-CSRF':status.csrf_token});
    setReceipt(`Read queued · job ${result.job_id}. Latest spectra update when it finishes.`);
  }catch(e){setError((e as Error).message);}finally{setReading(false);}};
  const card=(input:Input)=>{
    const b=input.board;
    return <article className={`snap-spectrum-card${input.beamforming?' in-beamforming':''}`} data-input-key={input.key} key={input.key}>
      <header><div><h4>{input.station??`ADC ${input.adc}`} {input.beamforming&&<span className="snap-bf-badge">Beamforming</span>}</h4><span>{input.antenna?`Ant ${input.antenna}`:'No wired antenna'} · SNAP {input.snap} SLOT {input.slot} ADC {input.adc}</span></div><button title="Hide this input" aria-label={`Hide SNAP ${input.snap} ADC ${input.adc}`} onClick={()=>toggle(input.key)}>−</button></header>
      <button className="snap-plot-trigger" aria-label={`Expand SNAP ${input.snap} ADC ${input.adc}`} onClick={()=>setExpanded(input.key)}>
        <SnapPlot freq={frame?.freq_mhz??[]} values={b.spectra_db?.[input.adc]??null} range={sharedRange} powerReference={powerReference}/>
      </button>
      <footer className={b.ts===null||b.error||b.stale?'snap-warning':''}>{b.ts===null?'No read in this snapshot':`${stamp(b.ts)}${b.stale?' · STALE':''}`}
        {!!b.zero_channels?.[input.adc]&&<span> · {b.zero_channels[input.adc]} zero-power channels</span>}
        {!!b.invalid_channels?.[input.adc]&&<span> · {b.invalid_channels[input.adc]} invalid channels</span>}
        {b.error&&<span> · Saved spectrum unavailable</span>}
      </footer>
    </article>;
  };
  const expandedInput=inputs.find(i=>i.key===expanded);
  return <div className="workspace-page snap-spectra-page">
    <div className="page-heading"><h2>SNAPs</h2><p className="muted">375–500 MHz · 4096 channels · latest spectra</p></div>
    {acquisition?.due&&<Notice>Read is due or overdue. Saved timestamps below show what was actually acquired.</Notice>}
    {[error,statusError,frameError].filter(Boolean).map((e,i)=><Notice key={i}>{e}</Notice>)}
    <SnapHealth data={health.data} error={health.error}/>
    <div className="snap-arrangement"><div className="snap-toolbar control-surface"><div className="snap-controls"><div className="field-row">
      <button className="primary" onClick={acquire} disabled={reading||!frame||!acquisition?.manual_enabled||!!acquisition?.manual_refusal}>{reading?'Queuing read…':'Ping now · get spectra'}</button>
      <span className="muted">Every {acquisition?acquisition.configured_interval_s/3600:'…'} h · page refresh 60 s</span>
    </div><div className="snap-job-status">{acquisition?.manual_refusal&&<span>{acquisition.manual_refusal.detail}</span>}{receipt&&<span role="status">{receipt}</span>}</div></div><div className="field-row">
      <div className="choice-row" aria-label="Panel order">{[['snap','Compact · SNAP order'],['station','Compact · station order'],['grid','Station grid']].map(([v,label])=><button key={v} aria-pressed={layout===v} className={layout===v?'active':''} onClick={()=>setLayout(v)}>{label}</button>)}</div>
      <div className="choice-row" aria-label="Input selection"><button className={scope==='beamforming'?'active':''} aria-pressed={scope==='beamforming'} onClick={()=>selectScope('beamforming')}>Beamforming · {membershipKnown?beamformingKeys.size:'unknown'}</button><button className={scope==='wired'?'active':''} aria-pressed={scope==='wired'} onClick={()=>selectScope('wired')}>All wired · {wired.length}</button></div>
      <button className={`snap-adc-toggle${allAdcs?' active':''}`} role="switch" aria-checked={allAdcs} onClick={()=>selectScope(allAdcs?'beamforming':'all')}><span className="snap-switch-track" aria-hidden="true"/><span>All 12 ADCs per SNAP<small>Include every input, wired or unwired</small></span><b>{allAdcs?'ON':'OFF'}</b></button>
      {Object.keys(overrides).length>0&&<button onClick={()=>setOverrides({})}>Reset selection</button>}
    </div>
    <p className="muted">{visible.length} panels · click a plot to enlarge and see its power trend.</p>
    </div>
    <details className="snap-layout-key" open><summary>Array layout <span>· {wired.length} wired · {membershipKnown?beamformingKeys.size:'?'} beamforming</span></summary><div className="snap-station-map"><div className="snap-map-columns"><strong>N ↑</strong>{[1,2,3,4,5,6].map(col=><span key={col}>E{col}</span>)}</div>{rows.map(row=><div key={row}><strong>N{String(row).padStart(2,'0')}</strong>{[1,2,3,4,5,6].map(col=>{const i=wired.find(a=>location(a).row===row&&location(a).col===col);return <button key={col} disabled={!i} aria-label={i?`${i.station} · Ant ${i.antenna}`:`N${row}E${col} · no antenna`} aria-pressed={!!i&&isVisible(i)} className={i?`${isVisible(i)?'shown':'panel-hidden'}${i.beamforming?' in-beamforming':''}`:''} onClick={()=>i&&toggle(i.key)} title={i?`${i.station} · Ant ${i.antenna} · SNAP ${i.snap} SLOT ${i.slot} ADC ${i.adc} · ${i.ip} · ${i.beamforming===null?'Beamforming unknown':i.beamforming?'In deployed CB weights':'Wired, outside deployed CB weights'}`:'No wired antenna'}>{i?i.antenna:'×'}</button>;})}</div>)}</div><p className="snap-map-legend"><span>Green: beamforming</span> · blue: other wired · faded: hidden · × empty</p></details>
    </div>
    {!membershipKnown&&<Notice>Deployed beamforming membership is unavailable. No antenna is assumed active; select All wired or All 12 ADCs to inspect spectra. {membership.error}</Notice>}
    {(frame?.boards??[]).filter((b:Json)=>Object.keys(b.latest_errors??{}).length).map((b:Json)=><details className="snap-board-errors" key={b.ip}><summary>SNAP {b.feng_id} · {b.ip} · control-read details · {stamp(b.latest_attempt)}</summary><p>A failed management read does not establish that the board is unprogrammed or has stopped sending data. Firmware and PPS alignment remain unverified when the control interface cannot be read.</p><pre>{JSON.stringify(b.latest_errors,null,2)}</pre><p>Original reader messages are preserved above. Spectra below retain their actual successful-read timestamps; no hardware recovery is attempted.</p></details>)}
    {!frame?<Notice>Loading saved full-band spectra…</Notice>:<>
      {layout==='snap'?(frame.boards??[]).map((b:Json)=><section className="snap-panel-group" key={b.ip}><header><h3>SNAP {b.feng_id} <span>SLOT {b.slot}</span></h3><span>{b.ip} · ADC order →</span></header><div className="snap-panel-grid">{visible.filter(i=>i.ip===b.ip).map(card)}</div></section>):rows.map(row=><section className="snap-panel-group" key={row}><header><h3>N{String(row).padStart(2,'0')}</h3><span>West → east · E1–E6</span></header><div className={`snap-panel-grid ${layout==='grid'?'snap-physical-grid':''}`}>{layout==='grid'?[1,2,3,4,5,6].map(col=>{const i=visible.find(a=>location(a).row===row&&location(a).col===col);return i?card(i):<div className="snap-empty" key={col}>E{col} · {wired.some(a=>location(a).row===row&&location(a).col===col)?'Hidden':'No antenna'}</div>;}):visible.filter(i=>location(i).row===row).sort((a,b)=>location(a).col-location(b).col).map(card)}</div></section>)}
      {layout!=='snap'&&allAdcs&&<section className="snap-panel-group"><header><h3>Unwired ADCs · SNAP order</h3></header><div className="snap-panel-grid">{visible.filter(i=>!i.functional||!i.station).map(card)}</div></section>}
    </>}
    <details className="snap-membership-evidence"><summary>Beamforming & plot details</summary><p>{membership.data?.note}</p><p>{membership.data?.path}</p><p>Deployment event: {membership.data?.live_event_utc??'unknown'} · payload inspected: {membership.data?.inspected_at??'not available'}</p><p>{membership.data?.evidence}</p><p>Shared axes: 374.9–500.1 MHz · {sharedRange[0]} to {sharedRange[1]} dB. Full data range, updated with the saved spectra and selection. Fixed reference: 10⁻¹⁰ native power, not dBm. History uses saved reads only.</p></details>
    {expandedInput&&frame&&<Expanded input={expandedInput} frame={frame} days={days} range={sharedRange} powerReference={powerReference} onClose={()=>setExpanded(null)}/>}
  </div>;
}
