import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, Evidence, initialWindow, isoInput, Json, Notice, SaveInvestigation, TimeWindow, TimeZone, LOCAL } from "../components/Workspace";

function ageText(seconds: number | null | undefined) {
  if (seconds == null) return "no gulps";
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds/60)} min`;
  return `${(seconds/3600).toFixed(1)} h`;
}

export default function T1Page() {
  const [window,setWindow]=useState(initialWindow()),[data,setData]=useState<Json|null>(null),[query,setQuery]=useState(""),[error,setError]=useState(""),[busy,setBusy]=useState(false);
  const [zone,setZone]=useState(LOCAL),[rolling,setRolling]=useState(true);
  const loading=useRef(false);
  const load=async()=>{if(loading.current)return;loading.current=true;setBusy(true);setError("");try{const q=new URLSearchParams({t0:isoInput(window.t0),t1:isoInput(window.t1),time_tz:zone}).toString();setData(await api("/api/t1?"+q));setQuery(q);}catch(e){setError((e as Error).message);}finally{loading.current=false;setBusy(false);}};
  useEffect(()=>{const timer=globalThis.setInterval(()=>{if(loading.current)return;globalThis.clearInterval(timer);void load();},350);return()=>globalThis.clearInterval(timer);},[window.t0,window.t1,zone]);
  useEffect(()=>{if(!rolling)return;const timer=globalThis.setInterval(()=>{if(!document.hidden&&!loading.current)setWindow(initialWindow());},300000);return()=>globalThis.clearInterval(timer);},[rolling]);
  const streams:Json[]=data?.streams??[];
  const capHits=streams.reduce((sum,s)=>sum+(s.cap_hits_last_hour??0),0);
  const capStreams=streams.filter(s=>(s.cap_hits_last_hour??0)>0).length;
  return <div className="workspace-page"><div className="page-heading"><p className="eyebrow">Search pressure and interference</p><h2>T1 · Hella streams</h2><p className="muted">Is every stream alive, and what did it find. Empty gulps are the normal state.</p></div><div className="control-surface"><div className="field-row"><TimeZone value={zone} onChange={setZone}/><button onClick={()=>{setRolling(true);setWindow(initialWindow());}}>Live · rolling 24 h</button><span>{rolling?"Updates every 5 minutes":"Historical interval"}</span></div><TimeWindow value={window} timeZone={zone} onChange={w=>{setRolling(false);setWindow(w);}}/><button className="primary" disabled={busy} onClick={load}>{busy?"Loading stream evidence…":"Inspect interval"}</button></div>{error&&<Notice>{error}</Notice>}{data&&<><div className="stream-strip">{streams.map((s:Json)=><div key={s.stream} className={`stream-cell ${s.status}`}><span>stream {s.stream}</span><em>{s.node}</em><strong>{ageText(s.last_gulp_age_s)}</strong><span>{s.status}</span><em>{s.gulps_last_hour}/{Math.round(s.expected_gulps_per_hour)} gulps/h</em><em>{s.empty_fraction_last_hour==null?"empty n/a":`empty ${Math.round(100*s.empty_fraction_last_hour)}%`}</em></div>)}</div><p className="muted strip-note">Ages are as of the log read {ageText(data.ledger?.read_age_s)} ago.</p><figure className="plot-surface"><figcaption>Stream liveness, gulp activity, beam and DM occupancy · {zone==="UTC"?"UTC":"OVRO local (PDT/PST)"}</figcaption><a href={data.plot_url??`/api/t1/plot.png?${query}`} target="_blank" rel="noreferrer"><img src={data.plot_url??`/api/t1/plot.png?${query}`} alt="Hella stream liveness, gulp activity, beam versus time, DM versus time and candidate histograms"/></a><div className="plot-actions"><a download href={data.plot_url??`/api/t1/plot.png?${query}`}>Download PNG</a><a download href={`/api/t1?${query}`}>Download evidence JSON</a><Link to={`/vis?${query}`}>Inspect these times in visibilities</Link></div><SaveInvestigation selection={{kind:"t1",...Object.fromEntries(new URLSearchParams(query))}} provenance={data.ledger??{}} plotUrl={data.plot_url??`/api/t1/plot.png?${query}`}/></figure><section className="control-surface"><p>Cap hits last hour: {capHits} across {capStreams} streams</p><p className="muted">Gulp ledger: {data.ledger?.status??"unknown"} · {data.ledger?.rows_in_window??0} gulps in this interval · {data.ledger?.log_path??"log path unknown"}</p><details><summary>Full evidence and coverage</summary><Evidence value={data}/></details></section></>} {!data&&!busy&&<div className="empty-science"><h3>Start with the interval that needs attention</h3><p>Eight streams, 64 beams each. A stream with no recorded gulps has stopped; a stream with gulps and no candidates is healthy.</p></div>}</div>;
}
