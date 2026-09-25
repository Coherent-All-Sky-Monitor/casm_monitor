import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, Evidence, initialWindow, isoInput, Json, LOCAL, Notice, TimeWindow, TimeZone } from "../components/Workspace";
import { InjectionPanel, localStamp, OverviewImage, VisibilityPreview } from "../components/OverviewPlots";
import { FigureZoom } from "../components/FigureZoom";
import { location, wiringLabel } from "../components/vis/ArrayPlots";
import telescopePhoto from "../assets/casm-telescope.jpg";
import "../overview.css";

type Health = "ok"|"late"|"silent"|"unknown"|"yellow"|"orange";
function useLive(url:string,period=30000) {
  const [data,setData]=useState<Json|null>(null),[error,setError]=useState('');
  useEffect(()=>{let active=true,busy=false;const load=async()=>{if(busy)return;busy=true;try{const value=await api(url);if(active){setData(value);setError('');}}catch(e){if(active)setError((e as Error).message);}finally{busy=false;}};void load();const timer=globalThis.setInterval(()=>{if(!document.hidden)void load();},period);return()=>{active=false;globalThis.clearInterval(timer);};},[url,period]);
  return {data,error};
}
function secondsSince(value:string|number|null|undefined,now:number) {if(value==null)return null;const t=typeof value==='number'?value*1000:Date.parse(value);return Number.isFinite(t)?Math.max(0,(now-t)/1000):null;}
function ageText(seconds:number|null) {return seconds==null?'Unknown':seconds<90?`${Math.round(seconds)} s`:`${Math.round(seconds/60)} min`;}
function Stat({title,value,note,status,label,to}:{title:string;value:string;note:string;status:Health;label:string;to:string}) {
  return <Link className={`overview-stat ${status}`} to={to}><span>{title}</span><strong>{value}</strong><small>{note}</small><b className="overview-status">{label}</b></Link>;
}

function ArrayContext({catalog,observation}:{catalog:Json;observation:Json|null}) {
  const inputs=catalog.inputs??[],selected=new Set(catalog.inspection_inputs??[]);
  const rows=[...new Set<number>(inputs.map((a:Json)=>location(a as any).row))].sort((a,b)=>b-a);
  const [focus,setFocus]=useState<number|null>(null);
  const antenna=inputs.find((a:Json)=>a.packet_idx===focus),cal=catalog.transit_calibrations?.[0];
  return <><div className="overview-panel-heading"><h3>Array & calibration</h3><span className="overview-now">Now</span></div>
    <div className="overview-array-counts"><strong>{selected.size}</strong><span>inspection set</span><b>{inputs.length}</b><span>wired</span><b>{inputs.filter((a:Json)=>a.in_bf).length}</b><span>intended for beams</span></div>
    <div className="overview-map" aria-label="Current station layout; north up, east right"><div className="overview-map-direction"><span>North ↑</span><span>East →</span></div><div className="overview-map-grid"><span/>{[1,2,3,4,5,6].map(c=><span key={c}>E{c}</span>)}{rows.map(row=><div className="overview-map-row" key={row}><span>N{String(row).padStart(2,'0')}</span>{[1,2,3,4,5,6].map(col=>{const a=inputs.find((i:Json)=>location(i as any).row===row&&location(i as any).col===col);return a?<button key={col} className={selected.has(a.packet_idx)?'inspected':''} aria-pressed={focus===a.packet_idx} aria-label={`${a.station}, antenna ${a.antenna}`} title={`${a.station} · ant ${a.antenna}`} onClick={()=>setFocus(a.packet_idx)}>{a.antenna}</button>:<span className="overview-map-empty" key={col} title={`N${row}E${col}: no wired antenna`}>×</span>;})}</div>)}</div></div>
    <p className="overview-caption">Purple: inspection set · outlined: other wired · × no wired antenna. Configuration, not antenna health.</p>
    <p className="overview-antenna-detail">{antenna?`${antenna.station} · ant ${antenna.antenna} · ${wiringLabel(antenna as any)}`:'Click an antenna for its station and SNAP / ADC.'}</p>
    <div className="overview-calibration"><span>Current ledger calibration</span><strong>{cal?.path?.split('/').pop()??'Unavailable'}</strong><small>Recorded weights membership: {observation?.layout?.counts?.deployed_union??'not yet verified'}</small></div>
    <div className="overview-panel-footer"><Link to="/vis">Inspect antennas →</Link><Link to="/cal">Calibration →</Link></div>
    <details className="overview-evidence"><summary>Configuration details</summary><p>Inspection membership is independent of beam deployment. Recorded weights membership does not confirm runtime activation.</p><Evidence value={{layout:catalog.current_layout,calibration:cal,deployment:observation?.deployment}}/></details>
  </>;
}

export default function OperationsPage() {
  const [photoOpen,setPhotoOpen]=useState(false);
  const live=useLive('/api/observation'),search=useLive('/api/t1/status'),catalog=useLive('/api/science/catalog',300000);
  const [now,setNow]=useState(Date.now()),[window,setWindow]=useState(initialWindow()),[zone,setZone]=useState(LOCAL),[rolling,setRolling]=useState(true);
  const [history,setHistory]=useState<Json|null>(null),[activity,setActivity]=useState<Json|null>(null),[historyError,setHistoryError]=useState(''),[activityError,setActivityError]=useState('');
  const t0=isoInput(window.t0),t1=isoInput(window.t1),query=new URLSearchParams({t0,t1,time_tz:zone}).toString();
  useEffect(()=>{const timer=globalThis.setInterval(()=>{if(!document.hidden)setNow(Date.now());},10000);return()=>globalThis.clearInterval(timer);},[]);
  useEffect(()=>{if(!rolling)return;const timer=globalThis.setInterval(()=>{if(!document.hidden)setWindow(initialWindow());},120000);return()=>globalThis.clearInterval(timer);},[rolling]);
  useEffect(()=>{
    let active=true;setHistory(null);setActivity(null);setHistoryError('');setActivityError('');
    const timer=globalThis.setTimeout(()=>{
      api('/api/observation/injections?'+query).then(d=>{if(active)setHistory(d);}).catch(e=>{if(active)setHistoryError(e.message);});
      api('/api/t1?compact=true&'+query).then(d=>{if(active)setActivity(d);}).catch(e=>{if(active)setActivityError(e.message);});
    },350);return()=>{active=false;globalThis.clearTimeout(timer);};
  },[query]);
  const data=live.data,inj=data?.injections,c=inj?.counts??{};
  const liveAge=secondsSince(data?.clock?.utc,now),stateAge=secondsSince(data?.observation?.state?.observed_at,now),state=data?.observation?.state?.value;
  const obsHealth:Health=live.error?'late':stateAge==null||state==null?'unknown':stateAge>120?'late':state==='running'?'ok':'silent';
  const streams:Json[]=search.data?.streams??[],tickAge=secondsSince(search.data?.ledger?.last_tick_unix,now);
  const searchFresh=!search.error&&tickAge!=null&&tickAge<=120,healthy=streams.filter(s=>s.status==='ok').length;
  const searchHealth:Health=search.error?'late':!streams.length||tickAge==null?'unknown':!searchFresh?'late':streams.some(s=>s.status==='silent')?'silent':streams.some(s=>s.status==='late')?'late':healthy===8?'ok':'unknown';
  const missed=(c.missed_t1??0)+(c.missed_t2??0),failures=c.fire_failed??0,injFresh=!live.error&&liveAge!=null&&liveAge<=120;
  // Operator thresholds are recovered counts over 24 h, not recovery percentages.
  // No completed evidence is not a measured zero; stale/partial counts take priority.
  const recoveryHealth:Health=!inj||inj.status==='unavailable'?'unknown':!injFresh||!inj.counts_complete?'late':!(c.completed_fired>0||failures>0)?((c.pending??0)+(c.unknown??0)>0?'late':'unknown'):c.recovered<12?'silent':c.recovered<=17?'orange':'yellow';
  const recoveryLabel=recoveryHealth==='yellow'?'Needs attention':recoveryHealth==='orange'?'Reduced recovery':recoveryHealth==='silent'?'Low recovery':recoveryHealth==='late'?'Pending / incomplete / stale':'No measurement';
  const vis=data?.observation?.vis_age,visMeasureAge=secondsSince(vis?.observed_at,now);
  const visAge=typeof vis?.value==='number'&&visMeasureAge!=null?vis.value+visMeasureAge:null;
  const visHealth:Health=live.error?'late':visAge==null?'unknown':visAge>600?'late':'ok';
  const alerts:{text:string;to:string}[]=[];
  if((data||live.error)&&obsHealth!=='ok')alerts.push({text:live.error?'Live observation refresh failed.':stateAge==null?'Observation state unavailable.':stateAge>120?'Observation state is stale.':`Observation state: ${state??'unknown'}.`,to:'/readiness'});
  if((search.data||search.error)&&searchHealth!=='ok')alerts.push({text:!searchFresh?'Search status unavailable or stale.':`${8-healthy} search streams need checking.`,to:'/search'});
  if(inj?.status==='ok'&&injFresh&&missed+failures>0)alerts.push({text:`Last 24 h: ${c.missed_t1??0} T1 misses · ${c.missed_t2??0} T2 misses · ${failures} firing failures.`,to:'/review'});
  if((data||live.error)&&recoveryHealth==='unknown')alerts.push({text:inj?.status==='unavailable'?'Injection ledger unavailable.':'No completed injection trials in the last 24 h.',to:'/cands?section=injections'});
  if(data&&visHealth!=='ok')alerts.push({text:`Cached visibility age: ${ageText(visAge)}.`,to:'/vis'});
  return <div className="workspace-page overview-page">
    <div className="overview-heading">
      <div className="overview-heading-copy"><h2>Overview</h2><p>Observation {data?.observation?.id??'unknown'}</p>
        <div className="overview-clocks">{data&&<><span>{localStamp(data.clock.utc,LOCAL)} OVRO local</span><span>{localStamp(data.clock.utc,'UTC')} UTC · LST {data.clock.lst??'unknown'}</span></>}</div>
      </div>
      <button className="overview-photo" aria-label="Enlarge CASM telescope photo" onClick={()=>setPhotoOpen(true)}>
        <img src={telescopePhoto} width="4032" height="3024" alt="Coherent All Sky Monitor antennas at Owens Valley Radio Observatory" decoding="async"/>
        <span>CASM <span aria-hidden="true">↗</span></span>
      </button>
    </div>
    {photoOpen&&<FigureZoom title="Coherent All Sky Monitor (CASM)" onClose={()=>setPhotoOpen(false)} actions={<><span>Owens Valley Radio Observatory · Bishop, California</span><a href={telescopePhoto} target="_blank" rel="noreferrer">Original photo</a></>}><img src={telescopePhoto} width="4032" height="3024" alt="Coherent All Sky Monitor antennas at Owens Valley Radio Observatory"/></FigureZoom>}
    <div className="overview-section-label"><span>Live snapshot · Now</span><span>Checked every 30 s while visible</span></div>
    <section className="overview-stats" aria-label="Live status">
      <Stat title="Observation" value={state??'Unknown'} note={`State checked ${ageText(stateAge)} ago`} status={obsHealth} label={obsHealth==='ok'?'Running':obsHealth==='late'?'Stale / check':obsHealth==='silent'?'Not running':'Unknown'} to="/readiness"/>
      <Stat title="Search streams" value={searchFresh?`${healthy} / 8`:'Unknown'} note="Streams with recent gulp evidence" status={searchHealth} label={searchHealth==='ok'?'All reporting':searchHealth==='late'?'Late / stale':searchHealth==='silent'?'Stream silent':'Unknown'} to="/search"/>
      <Stat title="Injection recovery · last 24 h" value={inj&&inj.status!=='unavailable'?`${c.recovered} / ${c.completed_fired}`:'Unavailable'} note="Recovered / completed fired trials" status={recoveryHealth} label={recoveryLabel} to="/review"/>
      <Stat title="Visibility data age" value={ageText(visAge)} note="Newest cached visibility · fresh ≤ 10 min" status={visHealth} label={visHealth==='ok'?'Recent data':visHealth==='late'?'Stale / check':'Unknown'} to="/vis"/>
    </section>
    {alerts.length>0&&<section className="overview-attention" aria-label="Needs attention"><strong>Needs attention</strong>{alerts.map(a=><Link key={a.text} to={a.to}>{a.text} →</Link>)}</section>}
    <section className="overview-controls"><div className="overview-control-line"><strong>{rolling?'Rolling 24 hours':'Historical interval'}</strong><span>{localStamp(t0,zone)} – {localStamp(t1,zone)}</span><div className="overview-history-actions"><TimeZone value={zone} onChange={setZone}/><button className={rolling?'active':''} onClick={()=>{setRolling(true);setWindow(initialWindow());}}>Live · rolling 24 h</button></div></div>
      <details><summary>Change history interval</summary><TimeWindow value={window} timeZone={zone} onChange={w=>{setRolling(false);setWindow(w);}}/></details><p>{rolling?'History plots refresh every 2 min.':'History is paused.'} Live status and configuration remain current.</p>
    </section>
    <div className="overview-main-grid"><section className="overview-panel overview-injections" aria-label="Injection recovery history">{history?<InjectionPanel data={history} zone={zone}/>:historyError?<Notice>{historyError}</Notice>:<p className="overview-loading">Loading injection history…</p>}</section>
      <section className="overview-panel overview-array">{catalog.data?<ArrayContext catalog={catalog.data} observation={data}/>:<Notice>{catalog.error||'Loading array configuration…'}</Notice>}{catalog.error&&catalog.data&&<Notice>Configuration refresh failed; the map may be stale.</Notice>}</section></div>
    <div className="overview-diagnostics"><section className="overview-panel overview-search"><div className="overview-panel-heading"><h3>Search activity</h3><span>8 streams</span></div>
      {activityError&&<Notice>{activityError}</Notice>}{!activity&&!activityError&&<p className="overview-loading">Loading search activity…</p>}
      {activity&&<>{activity.activity_plot_url?<OverviewImage src={activity.activity_plot_url} title="Search activity"/>:<Notice>Search figure unavailable.</Notice>}{activity.status==='partial'&&<Notice>{activity.reason}</Notice>}</>}
      <div className="overview-panel-footer"><span>Purple: counts · red strips: cap warnings</span><Link to={'/search?'+query}>Search (T1) →</Link></div>
    </section><section className="overview-panel overview-visibility">{catalog.data?<VisibilityPreview catalog={catalog.data} t0={t0} t1={t1} zone={zone}/>:<Notice>{catalog.error||'Loading visibility context…'}</Notice>}</section></div>
    <details className="overview-records"><summary>Observation records</summary><Evidence value={{observation:data?.observation,injection_ledger:inj?.source,history_complete:history?.counts_complete,history_start:history?.window_start_utc,history_end:history?.window_end_utc,search_ledger:search.data?.ledger}}/><a href="http://localhost:8050/" target="_blank" rel="noreferrer">T2 / T3 viewer →</a></details>
  </div>;
}
