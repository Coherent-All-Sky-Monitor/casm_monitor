import { useEffect, useState } from "react";
import { FigureZoom } from "./FigureZoom";

export type Json = Record<string, any>;
let renderQueue: Promise<unknown> = Promise.resolve();
export function api<T = Json>(url: string, body?: unknown, headers: Record<string,string> = {}): Promise<T> {
  if (url !== "/api/science/render") return requestApi<T>(url,body,headers);
  const next = renderQueue.then(()=>requestApi<T>(url,body,headers));
  renderQueue = next.catch(()=>undefined);
  return next;
}
async function requestApi<T = Json>(url: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
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
export { TimeWindow, TimeZone, LOCAL } from "./TimeControls";
export function Notice({children}: {children: React.ReactNode}) { return <p className="workspace-notice" role="status">{children}</p>; }
export function Evidence({value}: {value: unknown}) {return <details className="evidence"><summary>Selection and evidence</summary><pre>{JSON.stringify(value,null,2)}</pre></details>;}
export function SaveInvestigation({selection, provenance, plotUrl}: {selection: Json;provenance?: Json;plotUrl?: string}) {
  const [open,setOpen]=useState(false), [note,setNote]=useState(""), [message,setMessage]=useState(""), [busy,setBusy]=useState(false);
  const save = async()=>{setBusy(true);try {const q=await api("/api/review");await api("/api/review",{title:note.slice(0,100)||"Marked scientific plot",note,selection,provenance:provenance??{},plot_url:plotUrl},{"X-CASM-Review-CSRF":q.csrf_token});setMessage("Saved to review queue. No investigation started.");setOpen(false);}catch(e){setMessage((e as Error).message);}finally{setBusy(false);}};
  return <div className="mark-investigation"><button onClick={()=>setOpen(!open)}>Mark for investigation</button>{open&&<div className="annotation-form"><label>What looks unusual?<textarea value={note} onChange={e=>setNote(e.target.value)} maxLength={4000} placeholder="Describe the feature and the question to investigate."/></label><button className="primary" disabled={busy||!note.trim()} onClick={save}>Save plot and note</button><span className="muted">Queued for your review; no agent runs automatically.</span></div>}{message&&<p role="status">{message}</p>}</div>;
}
function ProductImage({url,label}:{url:string;label:string}) {
  const [open,setOpen]=useState(false);
  return <><button className="plot-image-trigger" aria-label={`Enlarge ${label}`} onClick={()=>setOpen(true)}><img src={url} alt={label}/></button>
    {open&&<FigureZoom title={label} onClose={()=>setOpen(false)} actions={<a href={url} target="_blank" rel="noreferrer">Open original PNG ↗</a>}><img src={url} alt={label} draggable={false}/></FigureZoom>}
  </>;
}
export function ProductPlots({product}: {product: Json}) {return <div className="product-plots">{(product.warnings??[]).map((w:string,i:number)=><Notice key={i}>{w}</Notice>)}{(product.images??[]).map((im:Json,i:number)=><figure className="plot-surface" key={im.url??i}><figcaption>{im.label??"Scientific diagnostic"}</figcaption><ProductImage url={im.url} label={im.label??"Scientific diagnostic"}/><div className="plot-actions"><a download href={im.url}>Download PNG</a>{product.data_url&&<a download href={product.data_url}>Download data</a>}{product.metadata_url&&<a href={product.metadata_url} target="_blank" rel="noreferrer">Provenance JSON</a>}</div><SaveInvestigation selection={product.selection??{product_id:product.id}} provenance={{...product.provenance,product_id:product.id,metadata_url:product.metadata_url}} plotUrl={im.url}/></figure>)}<Evidence value={product.provenance??product}/></div>;}
