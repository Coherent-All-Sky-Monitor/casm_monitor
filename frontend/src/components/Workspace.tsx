import { useEffect, useState } from "react";

export type Json = Record<string, any>;
export async function api<T = Json>(url: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
  const r = await fetch(url, body === undefined ? undefined : {method: "POST", headers: {"Content-Type": "application/json", "X-CASM-Workspace": "1", ...headers}, body: JSON.stringify(body)});
  if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(typeof e.detail === "string" ? e.detail : JSON.stringify(e.detail ?? `HTTP ${r.status}`)); }
  return r.json();
}
export function useResource(url: string, interval = 0) {
  const [data, setData] = useState<Json | null>(null), [error, setError] = useState("");
  useEffect(() => { let active = true; const load = () => api(url).then(d => {if(active) {setData(d); setError("");}}).catch(e => {if(active) setError(e.message);}); load(); const timer = interval ? window.setInterval(load, interval) : 0; return () => {active = false; window.clearInterval(timer);}; }, [url, interval]);
  return {data, error};
}
export function stamp(value: string | number | null | undefined) {
  if (value == null) return "Unknown";
  const d = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isFinite(d.getTime()) ? d.toISOString().replace("T", " ").slice(0,19) + " UTC" : String(value);
}
export const utcInput = (d: Date) => d.toISOString().slice(0,16);
export function initialWindow(hours = 24) { return {t0: utcInput(new Date(Date.now() - hours*3600000)), t1: utcInput(new Date())}; }
export function isoInput(s: string) {return new Date(s + (s.endsWith("Z") ? "" : "Z")).toISOString();}
export function TimeWindow({value, onChange}: {value: {t0: string;t1: string};onChange: (v: {t0:string;t1:string}) => void}) {
  const [day, setDay] = useState(new Date().toISOString().slice(0,10));
  const today = () => { const p = new Intl.DateTimeFormat("en-CA", {timeZone:"America/Los_Angeles",year:"numeric",month:"2-digit",day:"2-digit"}).formatToParts(new Date()); const get = (k:string) => p.find(x => x.type === k)?.value; const date = `${get("year")}-${get("month")}-${get("day")}`; const offset = new Intl.DateTimeFormat("en", {timeZone:"America/Los_Angeles",timeZoneName:"shortOffset"}).formatToParts(new Date()).find(x=>x.type==="timeZoneName")?.value; const h = Number(offset?.replace("GMT", "") || -8); onChange({t0:utcInput(new Date(Date.parse(date+"T00:00:00Z")-h*3600000)),t1:utcInput(new Date())}); };
  return <div className="time-controls"><div className="choice-row"><button onClick={today}>Today · OVRO</button>{[1,24,168].map(h=><button key={h} onClick={()=>onChange(initialWindow(h))}>{h===1?"Last hour":h===24?"Last 24 h":"Last week"}</button>)}<label>UTC day<input type="date" value={day} onChange={e=>{setDay(e.target.value);if(e.target.value) {const d = new Date(e.target.value+"T00:00Z");onChange({t0:utcInput(d),t1:utcInput(new Date(Math.min(Date.now(),d.getTime()+86400000)))});}}}/></label></div><div className="field-row"><label>From · UTC<input aria-label="From UTC" type="datetime-local" value={value.t0} onChange={e=>onChange({...value,t0:e.target.value})}/></label><label>To · UTC<input aria-label="To UTC" type="datetime-local" value={value.t1} onChange={e=>onChange({...value,t1:e.target.value})}/></label></div></div>;
}
export function Notice({children}: {children: React.ReactNode}) { return <p className="workspace-notice" role="status">{children}</p>; }
export function Evidence({value}: {value: unknown}) {return <details className="evidence"><summary>Selection and evidence</summary><pre>{JSON.stringify(value,null,2)}</pre></details>;}
export function SaveInvestigation({selection, provenance, plotUrl}: {selection: Json;provenance?: Json;plotUrl?: string}) {
  const [open,setOpen]=useState(false), [note,setNote]=useState(""), [message,setMessage]=useState(""), [busy,setBusy]=useState(false);
  const save = async()=>{setBusy(true);try {const q=await api("/api/review");await api("/api/review",{title:note.slice(0,100)||"Marked scientific plot",note,selection,provenance:provenance??{},plot_url:plotUrl},{"X-CASM-Review-CSRF":q.csrf_token});setMessage("Saved to review queue. No investigation started.");setOpen(false);}catch(e){setMessage((e as Error).message);}finally{setBusy(false);}};
  return <div className="mark-investigation"><button onClick={()=>setOpen(!open)}>Mark for investigation</button>{open&&<div className="annotation-form"><label>What looks unusual?<textarea value={note} onChange={e=>setNote(e.target.value)} maxLength={4000} placeholder="Describe the feature and the question to investigate."/></label><button className="primary" disabled={busy||!note.trim()} onClick={save}>Save plot and note</button><span className="muted">Queued for your review; no agent runs automatically.</span></div>}{message&&<p role="status">{message}</p>}</div>;
}
export function ProductPlots({product}: {product: Json}) {return <div className="product-plots">{(product.warnings??[]).map((w:string,i:number)=><Notice key={i}>{w}</Notice>)}{(product.images??[]).map((im:Json,i:number)=><figure className="plot-surface" key={im.url??i}><figcaption>{im.label??"Scientific diagnostic"}</figcaption><a href={im.url} target="_blank" rel="noreferrer"><img src={im.url} alt={im.label??"Scientific diagnostic"}/></a><div className="plot-actions"><a download href={im.url}>Download PNG</a>{product.data_url&&<a download href={product.data_url}>Download data</a>}{product.metadata_url&&<a href={product.metadata_url} target="_blank" rel="noreferrer">Provenance JSON</a>}</div><SaveInvestigation selection={product.selection??{product_id:product.id}} provenance={{...product.provenance,product_id:product.id,metadata_url:product.metadata_url}} plotUrl={im.url}/></figure>)}<Evidence value={product.provenance??product}/></div>;}
