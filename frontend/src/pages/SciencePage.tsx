import { useEffect, useMemo, useState, useRef } from "react";
import { useSearchParams } from "react-router-dom";
import { api, Evidence, initialWindow, isoInput, Json, Notice, ProductPlots, TimeWindow, TimeZone, LOCAL, useResource } from "../components/Workspace";

const KINDS = [["phase_waterfall","Phase waterfall"],["amplitude_waterfall","Amplitude waterfall"],["phase_spectrum","Phase / frequency"],["amplitude_spectrum","Amplitude spectrum"],["autos","Autocorrelations"]];
export default function SciencePage({compare = false}: {compare?:boolean}) {
  const [params,setParams]=useSearchParams();
  const [window,setWindow]=useState(()=>({t0:params.get("t0")?.slice(0,16)||initialWindow(compare?1:24).t0,t1:params.get("t1")?.slice(0,16)||initialWindow().t1}));
  const [kind,setKind]=useState(params.get("kind")||(compare?"phase_spectrum":"phase_waterfall"));
  const [reference,setReference]=useState(params.get("reference")||"sun"), [layout,setLayout]=useState("");
  const [fmin,setFmin]=useState(params.get("fmin")||"390.625"),[fmax,setFmax]=useState(params.get("fmax")||"484.375");
  const [resolution,setResolution]=useState(compare?"full":"avg8"),[filter,setFilter]=useState("long_ns"),[plank,setPlank]=useState(""),[length,setLength]=useState("");
  const [selected,setSelected]=useState<string[]>(()=>params.get("pairs")?.split(";").filter(Boolean)||[]),[product,setProduct]=useState<Json|null>(null),[error,setError]=useState(""),[busy,setBusy]=useState(false);
  const [zone,setZone]=useState(params.get("time_tz")||LOCAL),[rolling,setRolling]=useState(!compare&&!params.has("t0"));
  const rendering=useRef(false);
  const [comparison,setComparison]=useState(initialWindow(1));
  const {data:catalog,error:catalogError}=useResource("/api/science/catalog"+(layout?`?layout_id=${encodeURIComponent(layout)}`:""));
  const baselines:Json[]=catalog?.baselines??[];
  const key=(b:Json)=>`${b.i},${b.j}`;
  useEffect(()=>{if(catalog&&!selected.length)setSelected((catalog.default_pairs??[]).map((p:number[])=>p.join(",")));},[catalog]); // Initial geometry-based preset only.
  const visible=useMemo(()=>baselines.filter(b=>{
    const n=Math.abs(b.ns_m??0),e=Math.abs(b.ew_m??0);
    if(filter==="long_ns"&&!(n>=7&&e<=1.5))return false;
    if(filter==="ns"&&e>1.5)return false;
    if(filter==="same_row"&&n>0.5)return false;
    if(filter==="adjacent_row"&&!(n>0.5&&n<7))return false;
    if(plank&&!`${JSON.stringify(b.planks)} ${b.label}`.toLowerCase().includes(plank.toLowerCase()))return false;
    return !length||Math.abs(b.length_m-Number(length))<=0.8;
  }),[baselines,filter,plank,length]);
  const toggle=(k:string)=>setSelected(v=>v.includes(k)?v.filter(x=>x!==k):v.length<6?[...v,k]:v);
  const render=async(automatic=false)=>{if(rendering.current)return;rendering.current=true;setBusy(true);setError("");try {
    let pairs=selected.map(s=>s.split(",").map(Number));
    if(kind==="autos")pairs=Array.from(new Set(pairs.flat())).slice(0,6).map(i=>[i,i]);
    const request:Json={pairs,t0:isoInput(window.t0),t1:isoInput(window.t1),fmin:Number(fmin),fmax:Number(fmax),kind,reference,resolution,time_tz:zone};
    if(layout)request.layout_id=layout;
    if(compare){request.compare_t0=isoInput(comparison.t0);request.compare_t1=isoInput(comparison.t1);}
    const result=await api("/api/science/render",request);setProduct(result);
    if(!automatic)setParams({t0:request.t0,t1:request.t1,kind,reference,fmin,fmax,time_tz:zone,pairs:selected.join(";")},{replace:true});
  }catch(e){setError((e as Error).message);}finally{rendering.current=false;setBusy(false);}};
  useEffect(()=>{if(compare||resolution!=="avg8"||!selected.length)return;const timer=globalThis.setInterval(()=>{if(rendering.current)return;globalThis.clearInterval(timer);void render(true);},450);return()=>globalThis.clearInterval(timer);},[selected.join(";"),window.t0,window.t1,kind,reference,resolution,zone,layout,compare]);
  useEffect(()=>{if(!rolling||compare||resolution!=="avg8")return;const timer=globalThis.setInterval(()=>{if(!document.hidden&&!rendering.current)setWindow(initialWindow());},120000);return()=>globalThis.clearInterval(timer);},[rolling,compare,resolution]);
  return <div className="workspace-page"><div className="page-heading"><div><p className="eyebrow">{compare?"Calibration validity":"Visibility inspection"}</p><h2>{compare?"Compare the same baseline across days":"Baseline amplitude and phase"}</h2><p className="muted">{compare?"Compare Sun-fringe-stopped phase with the calibration day. Phase drift prompts investigation, not deployment.":"Select physical baselines, then inspect amplitude, phase and frequency structure. No raw acquisition or telescope changes."}</p></div></div>
    {(catalogError||error)&&<Notice>{error||catalogError}</Notice>}
    <div className="workspace-layout"><aside className="selection-panel"><h3>Baselines</h3><label>Geometry preset<select value={filter} onChange={e=>setFilter(e.target.value)}><option value="long_ns">Long N–S</option><option value="ns">North–south</option><option value="same_row">Same plank row</option><option value="adjacent_row">Nearby rows (&lt;7 m N–S)</option><option value="all">All available pairs</option></select></label>
      <div className="field-row"><label>Length ≈ m<input type="number" step="0.1" value={length} onChange={e=>setLength(e.target.value)} placeholder="10.5"/></label><label>Plank / station<input value={plank} onChange={e=>setPlank(e.target.value)} placeholder="e.g. N21"/></label></div>
      <p className="muted">{selected.length} selected · up to 6 panels. Length filter ±0.8 m.</p><div className="choice-row"><button onClick={()=>setSelected(visible.slice(0,3).map(key))}>Select first three</button><button onClick={()=>setSelected([])}>Clear</button></div>
      <div className="baseline-list">{visible.map(b=><label key={key(b)} className={selected.includes(key(b))?"baseline selected":"baseline"}><input type="checkbox" checked={selected.includes(key(b))} onChange={()=>toggle(key(b))}/><span><strong>{Number(b.length_m).toFixed(2)} m · {b.orientation??"baseline"}</strong><small>{[b.i,b.j].map(p=>catalog?.inputs?.find((i:Json)=>i.packet_idx===p)?.station??`Input ${p}`).join(" × ")}</small></span></label>)}{catalog&&!visible.length&&<p>No baselines match this geometry.</p>}{!catalog&&<p>Loading geometry…</p>}</div>
      <details><summary>Selected pairs and layout</summary><ul>{selected.map(s=><li key={s}>{baselines.find(b=>key(b)===s)?.label??s}</li>)}</ul><label>Layout epoch<select value={layout} onChange={e=>{setLayout(e.target.value);setSelected([]);}}><option value="">Current layout</option>{(catalog?.layouts??[]).map((l:Json)=><option key={l.id} value={l.id}>{l.date??l.id}</option>)}</select></label><Evidence value={{availability:catalog?.availability,limits:catalog?.limits,preset:catalog?.preset_source}}/></details>
    </aside><section className="science-work"><div className="control-surface"><div className="field-row"><TimeZone value={zone} onChange={setZone}/>{!compare&&<button onClick={()=>{setRolling(true);setResolution("avg8");setWindow(initialWindow());}}>Live · rolling 24 h</button>}</div><TimeWindow value={window} timeZone={zone} onChange={w=>{setRolling(false);setWindow(w);}}/><div className="choice-row quantity-tabs">{KINDS.map(([v,label])=><button key={v} className={kind===v?"active":""} onClick={()=>setKind(v)}>{label}</button>)}</div><div className="field-row"><label>Processing<select value={reference} onChange={e=>setReference(e.target.value)}><option value="sun">Sun fringe-stopped</option><option value="raw">Raw</option></select></label><label>Frequency min · MHz<input type="number" step="0.01" value={fmin} onChange={e=>setFmin(e.target.value)}/></label><label>Frequency max · MHz<input type="number" step="0.01" value={fmax} onChange={e=>setFmax(e.target.value)}/></label><label>Data source / resolution<select value={resolution} onChange={e=>setResolution(e.target.value)}><option value="avg8">8-channel average · week overview</option><option value="full">Native cache · up to 6 hours</option><option value="recorded">Native recording · up to 1 hour</option></select></label></div>
      {compare&&<details open><summary>Calibration-day comparison interval</summary><TimeWindow value={comparison} timeZone={zone} onChange={setComparison}/><p className="muted">Select the actual calibration window from its saved report or notebook on the Calibration page. Match source-transit geometry and processing. This does not apply the deployed calibration to today's data.</p></details>}
      <button className="primary" onClick={()=>void render()} disabled={busy||!selected.length}>{busy?"Reading selected data and rendering…":"Render selection"}</button><span className="muted"> {compare||resolution!=="avg8"?"Explicit render required for native reads and comparisons.":rolling?"Rolling 24 h · updates every two minutes.":"Cached plots update when selections change."}</span></div>
      {product&&<ProductPlots product={product}/>} {!product&&!busy&&<div className="empty-science"><h3>{compare?"Choose both observing windows":"Choose a few baselines to begin"}</h3><p>Your selected time and frequency range applies to every panel. Narrow the bounds and render again to inspect a feature.</p><p className="muted">Phase is meaningful only where there is signal. Missing coverage is not interpolated into an observation.</p></div>}
    </section></div></div>;
}
