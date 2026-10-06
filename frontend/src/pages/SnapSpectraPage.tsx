import { useMemo, useRef, useState } from 'react';
import { api, Json, Notice, stamp } from '../components/Workspace';
import {useCachedResource, useTabState} from '../components/CachedResource';
import { FigureZoom } from '../components/FigureZoom';
import { SnapPlot, SnapTrend } from '../components/SnapPlot';
import { dbOffset, snapRange, SNAP_REFERENCE_POWER } from '../components/snapPower';
import { SnapHealth } from '../components/SnapHealth';
import { SnapPlayback, PlaybackInput } from '../components/SnapPlayback';
import { SpectrumClocks } from '../components/SpectrumClocks';
import {PowerScale,linearPowerRange,powerLabel} from '../components/powerScale';
import {PowerScaleControl} from '../components/PowerScaleControl';
import {dbToLinear} from '../lib/spectrumUtils';
import {ChangeEvidence, previousSnapshot, SavedReadPicker, snapDay, SnapChanges, SnapSnapshot, snapshotForRead, snapshotUrl, useSnapResource} from '../components/SnapHistory';
import '../snap-spectra.css';

type Input = {key:string;ip:string;snap:number;slot:string;adc:number;station:string|null;antenna:number|null;functional:boolean;packet_idx:number|null;board:Json;beamforming:boolean|null};
const location=(i:Input)=>{const m=/N(\d+)E(\d+)/.exec(i.station??'');return {row:m?Number(m[1]):-1,col:m?Number(m[2]):-1};};
const useSelection=<T,>(key:string,initial:T)=>useTabState('snaps.'+key,initial);

function Expanded({input,frame,reference,days,range,powerReference,onClose,onPick,historical,powerScale,onPowerScale}:{input:Input;frame:Json;reference:Json|null;days:number;range:[number,number];powerReference:number;onClose:()=>void;onPick:(id:number,ts:number)=>void;historical:boolean;powerScale:PowerScale;onPowerScale:(scale:PowerScale)=>void}) {
  const end=historical&&input.board.ts?`&at=${(Math.floor(input.board.ts/86400)+1)*86400-1}&archive=true`:'';
  const {data:loadedTrend,error}=useSnapResource(`/api/snap-workspace/spectra-trend?ip=${encodeURIComponent(input.ip)}&adc=${input.adc}&days=${days}${end}`,historical?0:60000);
  const data=loadedTrend?.days===days&&loadedTrend?.ip===input.ip&&loadedTrend?.adc===input.adc?loadedTrend:null;
  const powers:number[]=(data?.points??[]).map((p:Json)=>p.power_db).filter((v:number|null)=>v!==null&&Number.isFinite(v)).map((v:number)=>powerScale==='linear'?dbToLinear(v)!:v+dbOffset(powerReference));
  const trendRange:[number,number]=powerScale==='linear'?linearPowerRange(powers):powers.length?[Math.floor(Math.min(...powers)/5)*5-2.5,Math.ceil(Math.max(...powers)/5)*5+2.5]:range;
  return <FigureZoom title={`${input.station??'Unwired input'} · SNAP ${input.snap} SLOT ${input.slot} ADC ${input.adc}`} onClose={onClose} controls={<PowerScaleControl value={powerScale} onChange={onPowerScale}/> }>
    <div className="snap-expanded"><SpectrumClocks at={input.board.ts} lst={input.board.lst}/>
      {reference&&<p className="snap-comparison-caption">Blue: {stamp(input.board.ts)} · orange dashed: {reference.ts?stamp(reference.ts):'No comparison read for this board'}{reference.ts&&input.board.ts&&reference.eq_epoch!==input.board.eq_epoch?' · EQ/FFT settings differ':''}</p>}
      <SnapPlot freq={frame.freq_mhz} values={input.board.spectra_db?.[input.adc]??null} reference={reference?.spectra_db?.[input.adc]} range={range} powerReference={powerReference} powerScale={powerScale}/>
      <h4>Full-band power · {days}-day history</h4><p>Mean power across all 4096 channels · {powerScale==='db'?'dB':'native power'} · click a point to open its spectrum</p>
      {data?.history_start&&<p className="muted">History since {stamp(data.history_start)}</p>}
      {error?<Notice>{error}</Notice>:data?<><SnapPlot freq={[]} values={null} trend={data as SnapTrend} range={trendRange} powerReference={powerReference} powerScale={powerScale} selectedAt={input.board.ts} onSelect={onPick}/><p>{data.points.length} saved reads · {data.points.filter((p:Json)=>p.power_db===null).length} incomplete / zero-power samples</p></>:<p>Reading saved history…</p>}
    </div>
  </FigureZoom>;
}

export default function SnapSpectraPage() {
  const [layout,setLayout]=useSelection('layout','snap'),[scope,setScope]=useSelection('scope','beamforming'),[overrides,setOverrides]=useSelection<Record<string,boolean>>('overrides',{}),[expanded,setExpanded]=useState<string|null>(null);
  const days=30,powerReference=SNAP_REFERENCE_POWER;
  const [powerScale,setPowerScale]=useSelection<PowerScale>('powerScale','db');
  const [mode,setMode]=useSelection<'live'|'history'>('mode','live'),[selected,setSelected]=useSelection<SnapSnapshot|null>('selected',null);
  const [comparing,setComparing]=useSelection('comparing',false),[comparison,setComparison]=useSelection<SnapSnapshot|null>('comparison',null);
  const [comparisonLoading,setComparisonLoading]=useState(false),[changeEvidence,setChangeEvidence]=useState<Json|null>(null);
  const [playback,setPlayback]=useState<{day:string;inputs:PlaybackInput[]}|null>(null);
  const allAdcs=scope==='all';
  const [reading,setReading]=useState(false),[receipt,setReceipt]=useState(''),[error,setError]=useState('');
  const {data:acquisition,error:statusError}=useCachedResource('/api/snap-workspace/acquisition',10000);
  const health=useCachedResource('/api/snap-workspace/health',30000,{allowStale:false});
  const membership=useCachedResource('/api/snap-workspace/beamforming',30000,{allowStale:false});
  const membershipKnown=!membership.error&&membership.data?.status==='complete';
  const beamformingKeys=useMemo(()=>new Set<string>(membershipKnown?(membership.data?.inputs??[]).filter((i:Json)=>i.beamforming===true).map((i:Json)=>`${i.ip}/${i.adc}`):[]),[membershipKnown,membership.data]);
  // Retain the displayed selection during membership revalidation, but remove
  // its green membership claim until the new check succeeds.
  const previousMembers=useRef(new Set<string>());
  if(membershipKnown)previousMembers.current=beamformingKeys;
  const revision=acquisition?.latest_job?.finished??'';
  const {data:frame,error:frameError}=useSnapResource(mode==='live'?(acquisition||statusError?`/api/snap-workspace/spectra?v=${revision}`:null):selected?snapshotUrl(selected):null,mode==='live'&&!playback?60000:0);
  const compare=useSnapResource(comparing&&comparison?snapshotUrl(comparison):null);
  const referenceFor=(ip:string)=>comparing?compare.data?.boards?.find((b:Json)=>b.ip===ip)??null:null;
  const changes=useSnapResource('/api/snap-workspace/changes',60000);
  const inputs:Input[]=useMemo(()=>(frame?.boards??[]).flatMap((board:Json)=>board.inputs.map((i:Json)=>({...i,key:`${board.ip}/${i.adc}`,ip:board.ip,snap:board.feng_id,slot:board.slot,board,beamforming:membershipKnown?beamformingKeys.has(`${board.ip}/${i.adc}`):null}))),[frame,membershipKnown,beamformingKeys]);
  const wired=inputs.filter(i=>i.functional&&i.station);
  const isVisible=(i:Input)=>overrides[i.key]??(allAdcs||(scope==='wired'?i.functional:membershipKnown?i.beamforming===true:previousMembers.current.has(i.key)));
  const visible=inputs.filter(isVisible);
  const rows=[...new Set(wired.map(i=>location(i).row))].sort((a,b)=>b-a);
  const sharedRange=useMemo(()=>{
    const values:(number|null)[]=[];
    for(const input of visible){values.push(...(input.board.spectra_db?.[input.adc]??[]));values.push(...(referenceFor(input.ip)?.spectra_db?.[input.adc]??[]));}
    return powerScale==='linear'?linearPowerRange(values.map(dbToLinear)):snapRange(values,powerReference);
  },[visible,powerReference,powerScale,compare.data,comparing]);
  const pick=async(id:number,ts:number)=>{setError('');try{const snapshot=await snapshotForRead(id,ts);setSelected(snapshot);setMode('history');}catch(e){setError((e as Error).message);}};
  const inspectChange=(item:Json)=>setChangeEvidence(item);
  const toggleCompare=async()=>{
    if(comparing){setComparing(false);return;}
    setComparing(true);if(comparison)return;
    setComparisonLoading(true);setError('');
    try{
      const board=frame?.boards?.find((b:Json)=>b.shard_id);
      const primary=mode==='history'&&selected?selected:board?await snapshotForRead(board.shard_id,board.ts):null;
      const prior=primary?await previousSnapshot(primary):null;
      if(prior)setComparison(prior);else{setComparing(false);setError('No earlier saved read is available for comparison.');}
    }catch(e){setError((e as Error).message);setComparing(false);}finally{setComparisonLoading(false);}
  };
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
    const stale=b.stale||(!b.historical&&b.ts!=null&&Date.now()/1000-b.ts>1.5*(frame?.configured_interval_s??3600));
    const reference=referenceFor(input.ip);
    const change=mode==='live'?changes.data?.inputs?.find((i:Json)=>i.ip===input.ip&&i.adc===input.adc):null;
    return <article className={`snap-spectrum-card${input.beamforming?' in-beamforming':''}`} data-input-key={input.key} key={input.key}>
      <header><div><h4>{input.station??`ADC ${input.adc}`} {input.beamforming&&<span className="snap-bf-badge">Beamforming</span>}</h4><span>{input.antenna?`Ant ${input.antenna}`:'No wired antenna'} · SNAP {input.snap} SLOT {input.slot} ADC {input.adc}</span></div><button title="Hide this input" aria-label={`Hide SNAP ${input.snap} ADC ${input.adc}`} onClick={()=>toggle(input.key)}>−</button></header>
      <button className="snap-plot-trigger" aria-label={`Expand SNAP ${input.snap} ADC ${input.adc}`} onClick={()=>setExpanded(input.key)}>
        <SnapPlot freq={frame?.freq_mhz??[]} values={b.spectra_db?.[input.adc]??null} reference={reference?.spectra_db?.[input.adc]} range={sharedRange} powerReference={powerReference} powerScale={powerScale}/>
      </button>
      <footer className={b.ts===null||b.error||stale?'snap-warning':''}>{b.ts===null?'No read in this snapshot':`${stamp(b.ts)}${stale?' · STALE':''}`}
        {!!b.zero_channels?.[input.adc]&&<span> · {b.zero_channels[input.adc]} zero-power channels</span>}
        {!!b.invalid_channels?.[input.adc]&&<span> · {b.invalid_channels[input.adc]} invalid channels</span>}
        {b.error&&<span> · Saved spectrum unavailable</span>}
        {comparing&&<span className="snap-reference-caption">Orange: {reference?.ts?stamp(reference.ts):'no comparison read'}{reference?.ts&&b.ts&&reference.eq_epoch!==b.eq_epoch?' · EQ/FFT differs':''}</span>}
        {change&&input.functional&&!['stable','info'].includes(change.state)&&<button className={`snap-change-badge ${change.state}`} onClick={()=>inspectChange(change)}>{change.state==='attention'?'Repeated · ':''}{change.label}</button>}
      </footer>
    </article>;
  };
  const expandedInput=inputs.find(i=>i.key===expanded);
  const openPlayback=()=>{
    if(!visible.length)return;
    const savedTimes=(frame?.boards??[]).map((b:Json)=>b.ts).filter((t:unknown):t is number=>typeof t==='number'&&Number.isFinite(t));
    const day=mode==='history'&&selected?selected.day:snapDay(savedTimes.length?Math.max(...savedTimes):Date.now()/1000);
    const ordered=[...visible];
    if(layout!=='snap')ordered.sort((a,b)=>location(b).row-location(a).row||location(a).col-location(b).col);
    setPlayback({day,inputs:ordered.map(({key,ip,adc,snap,slot,station,antenna,beamforming})=>({key,ip,adc,snap,slot,station,antenna,beamforming}))});
  };
  return <div className="workspace-page snap-spectra-page">
    <div className="page-heading"><h2>SNAPs</h2><p className="muted">375–500 MHz · 4096 channels · {mode==='live'?'latest spectra':'saved spectra'}</p></div>
    <section className="snap-history-bar control-surface" aria-label="Spectrum history controls">
      <div className="field-row"><div className="choice-row" aria-label="Spectrum mode"><button className={mode==='live'&&!playback?'active':''} aria-pressed={mode==='live'&&!playback} onClick={()=>{setPlayback(null);setMode('live');}}>Live</button><button className={mode==='history'||playback?'active':''} aria-pressed={mode==='history'||!!playback} disabled={!visible.length} onClick={openPlayback}>History</button></div>
      <span className="muted">{mode==='live'?'Latest saved spectra · updated hourly':selected?`${selected.day} · ${stamp(selected.at).slice(11,16)} UTC`:'Choose a date, then slide through saved times'}</span>
      <button className={`snap-compare-toggle${comparing?' active':''}`} aria-pressed={comparing} disabled={comparisonLoading||(!frame&&!comparing)} onClick={()=>void toggleCompare()}>{comparing?'Close comparison':'Compare with…'}</button></div>
      {mode==='history'&&frame&&<SpectrumClocks at={frame.at} lst={frame.lst} dark/>}
      {mode==='history'&&<SavedReadPicker label="Spectrum" value={selected} onChange={setSelected}/>}
      {comparing&&<div className="snap-comparison-disclosure">{comparisonLoading?<p>Finding the previous saved read…</p>:<SavedReadPicker label="Comparison" value={comparison} onChange={setComparison}/>}<p className="snap-comparison-caption">Blue: {mode==='live'?'latest':'selected'} read · orange dashed: comparison</p>{compare.error&&<Notice>{compare.error}</Notice>}</div>}
    </section>
    {acquisition?.due&&<Notice>Read is due or overdue. Saved timestamps below show what was actually acquired.</Notice>}
    {[error,statusError,frameError?frameError+(frame?' Showing the last successful spectrum at its labelled time.':''):''].filter(Boolean).map((e,i)=><Notice key={i}>{e}</Notice>)}
    <SnapHealth data={health.data} error={health.error}/>
    <div className="snap-arrangement"><div className="snap-toolbar control-surface"><div className="snap-controls"><div className="field-row">
      <button className="primary" onClick={acquire} disabled={reading||!frame||!acquisition?.manual_enabled||!!acquisition?.manual_refusal}>{reading?'Queuing read…':'Ping now · get spectra'}</button>
      <span className="muted">Every {acquisition?acquisition.configured_interval_s/3600:'…'} h · page refresh 60 s</span>
    </div><div className="snap-job-status">{acquisition?.manual_refusal&&<span>{acquisition.manual_refusal.detail}</span>}{receipt&&<span role="status">{receipt}</span>}</div></div><div className="field-row">
      <div className="choice-row" aria-label="Panel order">{[['snap','Compact · SNAP order'],['station','Compact · station order'],['grid','Station grid']].map(([v,label])=><button key={v} aria-pressed={layout===v} className={layout===v?'active':''} onClick={()=>setLayout(v)}>{label}</button>)}</div>
      <PowerScaleControl value={powerScale} onChange={setPowerScale}/>
      <div className="choice-row" aria-label="Input selection"><button className={scope==='beamforming'?'active':''} aria-pressed={scope==='beamforming'} onClick={()=>selectScope('beamforming')}>Beamforming · {membershipKnown?beamformingKeys.size:'unknown'}</button><button className={scope==='wired'?'active':''} aria-pressed={scope==='wired'} onClick={()=>selectScope('wired')}>All wired · {wired.length}</button></div>
      <button className={`snap-adc-toggle${allAdcs?' active':''}`} role="switch" aria-checked={allAdcs} onClick={()=>selectScope(allAdcs?'beamforming':'all')}><span className="snap-switch-track" aria-hidden="true"/><span>All 12 ADCs per SNAP<small>Include every input, wired or unwired</small></span><b>{allAdcs?'ON':'OFF'}</b></button>
      {Object.keys(overrides).length>0&&<button onClick={()=>setOverrides({})}>Reset selection</button>}
    </div>
    <SnapChanges data={changes.data} error={changes.error} onInspect={item=>void inspectChange(item)}/>
    <p className="muted">{visible.length} panels · click a plot to enlarge and see its power trend.</p>
    </div>
    <details className="snap-layout-key" open><summary>Array layout <span>· {wired.length} wired · {membershipKnown?beamformingKeys.size:'?'} beamforming</span></summary><div className="snap-station-map"><div className="snap-map-columns"><strong>N ↑</strong>{[1,2,3,4,5,6].map(col=><span key={col}>E{col}</span>)}</div>{rows.map(row=><div key={row}><strong>N{String(row).padStart(2,'0')}</strong>{[1,2,3,4,5,6].map(col=>{const i=wired.find(a=>location(a).row===row&&location(a).col===col);return <button key={col} disabled={!i} aria-label={i?`${i.station} · Ant ${i.antenna}`:`N${row}E${col} · no antenna`} aria-pressed={!!i&&isVisible(i)} className={i?`${isVisible(i)?'shown':'panel-hidden'}${i.beamforming?' in-beamforming':''}`:''} onClick={()=>i&&toggle(i.key)} title={i?`${i.station} · Ant ${i.antenna} · SNAP ${i.snap} SLOT ${i.slot} ADC ${i.adc} · ${i.ip} · ${i.beamforming===null?'Beamforming unknown':i.beamforming?'In deployed CB weights':'Wired, outside deployed CB weights'}`:'No wired antenna'}>{i?i.antenna:'×'}</button>;})}</div>)}</div><p className="snap-map-legend"><span>Green: beamforming</span> · blue: other wired · faded: hidden · × empty</p></details>
    </div>
    {!membershipKnown&&<Notice>Checking deployed beamforming membership. {previousMembers.current.size?'Keeping the previous panel selection; green membership marks return after a successful check.':'Select All wired or All 12 ADCs to inspect spectra while membership is unavailable.'} {membership.error}</Notice>}
    {(frame?.boards??[]).filter((b:Json)=>Object.keys(b.latest_errors??{}).length).map((b:Json)=><details className="snap-board-errors" key={b.ip}><summary>SNAP {b.feng_id} · {b.ip} · control-read details · {stamp(b.latest_attempt)}</summary><p>A failed management read does not establish that the board is unprogrammed or has stopped sending data. Firmware and PPS alignment remain unverified when the control interface cannot be read.</p><pre>{JSON.stringify(b.latest_errors,null,2)}</pre><p>Original reader messages are preserved above. Spectra below retain their actual successful-read timestamps; no hardware recovery is attempted.</p></details>)}
    {!frame?<Notice>{mode==='history'&&!selected?'Choose a date with saved reads, then select a time above.':'Loading saved full-band spectra…'}</Notice>:<>
      {layout==='snap'?(frame.boards??[]).map((b:Json)=><section className="snap-panel-group" key={b.ip}><header><h3>SNAP {b.feng_id} <span>SLOT {b.slot}</span></h3><span>{b.ip} · ADC order →</span></header><div className="snap-panel-grid">{visible.filter(i=>i.ip===b.ip).map(card)}</div></section>):rows.map(row=><section className="snap-panel-group" key={row}><header><h3>N{String(row).padStart(2,'0')}</h3><span>West → east · E1–E6</span></header><div className={`snap-panel-grid ${layout==='grid'?'snap-physical-grid':''}`}>{layout==='grid'?[1,2,3,4,5,6].map(col=>{const i=visible.find(a=>location(a).row===row&&location(a).col===col);return i?card(i):<div className="snap-empty" key={col}>E{col} · {wired.some(a=>location(a).row===row&&location(a).col===col)?'Hidden':'No antenna'}</div>;}):visible.filter(i=>location(i).row===row).sort((a,b)=>location(a).col-location(b).col).map(card)}</div></section>)}
      {layout!=='snap'&&allAdcs&&<section className="snap-panel-group"><header><h3>Unwired ADCs · SNAP order</h3></header><div className="snap-panel-grid">{visible.filter(i=>!i.functional||!i.station).map(card)}</div></section>}
    </>}
    <details className="snap-membership-evidence"><summary>Beamforming & plot details</summary><p>{membership.data?.note}</p><p>{membership.data?.path}</p><p>Deployment event: {membership.data?.live_event_utc??'unknown'} · payload inspected: {membership.data?.inspected_at??'not available'}</p><p>{membership.data?.evidence}</p><p>Shared axes: 374.9–500.1 MHz · {powerLabel(powerScale,'native')}. Full data range, updated with the saved spectra and selection. Fixed dB reference: 10⁻¹⁰ native power, not dBm. Linear shows native power without this reference offset. Unavailable channels stay blank in both views. History uses saved reads only.</p></details>
    {expandedInput&&frame&&<Expanded input={expandedInput} frame={frame} reference={referenceFor(expandedInput.ip)} days={days} range={sharedRange} powerReference={powerReference} powerScale={powerScale} onPowerScale={setPowerScale} historical={mode==='history'} onPick={(id,ts)=>void pick(id,ts)} onClose={()=>setExpanded(null)}/>}
    {changeEvidence&&<ChangeEvidence item={changeEvidence} onClose={()=>setChangeEvidence(null)}/>}
    {playback&&<SnapPlayback {...playback} powerScale={powerScale} onPowerScale={setPowerScale} onClose={()=>setPlayback(null)} onSelect={s=>{setSelected(s);setMode('history');}}/>}
  </div>;
}
