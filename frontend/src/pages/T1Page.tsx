import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { FigureZoom } from "../components/FigureZoom";
import { api, Evidence, initialWindow, isoInput, Json, Notice, SaveInvestigation, TimeWindow, TimeZone, LOCAL } from "../components/Workspace";
import "../search.css";

function ageText(seconds: number | null | undefined) {
  if (seconds == null) return "no gulps";
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds/60)} min`;
  return `${(seconds/3600).toFixed(1)} h`;
}

const STREAM_STATUS_LABEL: Record<string,string> = {ok:"OK",late:"Late",silent:"Silent",unknown:"Unknown"};

export default function T1Page() {
  const [window,setWindow]=useState(initialWindow()),[data,setData]=useState<Json|null>(null),[query,setQuery]=useState(""),[error,setError]=useState(""),[busy,setBusy]=useState(false);
  const [zone,setZone]=useState(LOCAL),[rolling,setRolling]=useState(true);
  const [zoom,setZoom]=useState<{url:string;query:string}|null>(null);
  const loading=useRef(false);
  const load=async()=>{if(loading.current)return;loading.current=true;setBusy(true);setError("");try{const q=new URLSearchParams({t0:isoInput(window.t0),t1:isoInput(window.t1),time_tz:zone}).toString();setData(await api("/api/t1?"+q));setQuery(q);}catch(e){setError((e as Error).message);}finally{loading.current=false;setBusy(false);}};
  useEffect(()=>{const timer=globalThis.setInterval(()=>{if(loading.current)return;globalThis.clearInterval(timer);void load();},350);return()=>globalThis.clearInterval(timer);},[window.t0,window.t1,zone]);
  useEffect(()=>{if(!rolling)return;const timer=globalThis.setInterval(()=>{if(!document.hidden&&!loading.current)setWindow(initialWindow());},300000);return()=>globalThis.clearInterval(timer);},[rolling]);
  const streams:Json[]=data?.streams??[];
  const capHits=streams.reduce((sum,s)=>sum+(s.cap_hits_last_hour??0),0);
  const capStreams=streams.filter(s=>(s.cap_hits_last_hour??0)>0).length;
  const plotUrl=data?.plot_url??`/api/t1/plot.png?${query}`;
  return <div className="workspace-page search-t1-page">
    <div className="page-heading"><h2>Search (T1)</h2><p className="muted">Hella · 8 streams · 64 beams per stream</p></div>
    <div className="control-surface">
      <div className="field-row"><TimeZone value={zone} onChange={setZone}/><button onClick={()=>{setRolling(true);setWindow(initialWindow());}}>Live · rolling 24 h</button><span>{rolling?"Updates every 5 minutes":"Historical interval"}</span></div>
      <TimeWindow value={window} timeZone={zone} onChange={w=>{setRolling(false);setWindow(w);}}/>
      <button className="primary" disabled={busy} onClick={load}>{busy?"Loading stream evidence…":"Inspect interval"}</button>
    </div>
    {error&&<Notice>{error}</Notice>}
    {data&&<>
      <section className="stream-strip" aria-label="Stream status">{streams.map((s:Json)=><article key={s.stream} className={`stream-cell ${s.status}`} aria-label={`Stream ${s.stream}`}>
        <header className="stream-heading"><h3>Stream {s.stream}</h3><span className="stream-node">{s.node}</span></header>
        <div className="stream-age"><span className="stream-label">Last gulp</span><div className="stream-reading">
          <strong>{ageText(s.last_gulp_age_s)}</strong><span className="stream-status">{STREAM_STATUS_LABEL[s.status]??"Unknown"}</span>
        </div></div>
        <dl className="stream-metrics">
          <div><dt>Gulps/h</dt><dd title="Observed / expected gulps in the last hour">{s.gulps_last_hour}<span className="stream-expected"> / {Math.round(s.expected_gulps_per_hour)}</span></dd></div>
          <div><dt>Empty gulps</dt><dd>{s.empty_fraction_last_hour==null?"n/a":`${Math.round(100*s.empty_fraction_last_hour)}%`}</dd></div>
        </dl>
      </article>)}</section>
      <p className="muted strip-note">Ages are as of the log read {ageText(data.ledger?.read_age_s)} ago.</p>
      <figure className="plot-surface">
        <figcaption>Stream, beam and DM candidate counts · {data.time_tz==="UTC"?"UTC":"OVRO local (PDT/PST)"} · click to enlarge</figcaption>
        <button type="button" className="plot-image-trigger" aria-label="Enlarge Search (T1) plots" onClick={()=>setZoom({url:plotUrl,query})}>
          <img src={plotUrl} alt="Hella candidate counts by stream, beam and DM over time, with width and DM histograms"/>
        </button>
        <div className="plot-actions"><a download href={plotUrl}>Download PNG</a><a download href={`/api/t1?${query}`}>Download evidence JSON</a><Link to={`/vis?${query}`}>Inspect these times in visibilities</Link></div>
        <SaveInvestigation selection={{kind:"t1",...Object.fromEntries(new URLSearchParams(query))}} provenance={data.ledger??{}} plotUrl={plotUrl}/>
      </figure>
      <section className="control-surface"><p>Cap hits last hour: {capHits} across {capStreams} streams</p>
        <p className="muted">Gulp ledger: {data.ledger?.status??"unknown"} · {data.ledger?.rows_in_window??0} gulps in this interval · {data.ledger?.log_path??"log path unknown"}</p>
        <details><summary>Full evidence and coverage</summary><Evidence value={data}/></details>
      </section>
    </>}
    {!data&&!busy&&<div className="empty-science"><h3>Start with the interval that needs attention</h3><p>Eight streams, 64 beams each. Empty gulps show that a stream ran; no recorded gulps means missing liveness evidence.</p></div>}
    {zoom&&<FigureZoom title="Search (T1) · Hella" onClose={()=>setZoom(null)} actions={<>
      <a href={zoom.url} target="_blank" rel="noreferrer">Open original PNG ↗</a><a download href={zoom.url}>Download PNG</a>
      <Link to={`/vis?${zoom.query}`}>Inspect these times in visibilities</Link>
    </>}><img src={zoom.url} alt="Search (T1) snapshot: stream, beam and DM candidate counts and histograms" draggable={false}/></FigureZoom>}
  </div>;
}
