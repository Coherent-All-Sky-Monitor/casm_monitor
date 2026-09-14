import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, initialWindow, isoInput, Json, LOCAL, Notice, ProductPlots, TimeWindow, TimeZone } from "./Workspace";

export default function MonitoringOverview() {
  const [window,setWindow] = useState(initialWindow());
  const [zone,setZone] = useState(LOCAL), [rolling,setRolling] = useState(true);
  const [data,setData] = useState<Json|null>(null), [error,setError] = useState(""), [busy,setBusy] = useState(false);
  useEffect(()=>{
    if (!rolling) return;
    const timer = globalThis.setInterval(()=>{if(!document.hidden)setWindow(initialWindow());},120000);
    return ()=>globalThis.clearInterval(timer);
  },[rolling]);
  useEffect(()=>{
    let active = true;
    const load = async()=>{
      setBusy(true);setError("");
      try {
        const catalog = await api("/api/science/catalog");
        if (!active) return;
        const selection = {pairs:catalog.default_pairs.slice(0,1),t0:isoInput(window.t0),t1:isoInput(window.t1),fmin:390.625,fmax:484.375,reference:"sun",resolution:"avg8",time_tz:zone};
        const results:Json = {selection,preset:catalog.preset_source};
        const q = new URLSearchParams({t0:selection.t0,t1:selection.t1,time_tz:zone});
        try {results.t1=await api("/api/t1?"+q);}catch(e){results.t1={error:(e as Error).message};}
        if(active)setData({...results});
        // Independent errors keep search monitoring visible if visibility coverage is absent.
        for (const kind of ["phase_waterfall","amplitude_waterfall"]) {
          if (!active) return;
          try {results[kind]=await api("/api/science/render",{...selection,kind});}
          catch(e) {results[kind]={error:(e as Error).message};}
          if(active)setData({...results});
        }
        if(!active)return;

      } catch(e) {if(active)setError((e as Error).message);}
      finally {if(active)setBusy(false);}
    };
    const debounce = globalThis.setTimeout(load,350);
    return ()=>{active=false;globalThis.clearTimeout(debounce);};
  },[window.t0,window.t1,zone]);
  return <section className="monitoring-overview"><div className="section-heading"><h3>Observation monitoring · {rolling?"rolling 24 hours":"selected interval"}</h3><span className="muted">{busy?"Updating plots…":rolling?"Updates every two minutes while visible":"Historical selection paused"}</span></div>
    <div className="control-surface"><div className="field-row"><TimeZone value={zone} onChange={setZone}/><button className={rolling?"active":""} onClick={()=>{setRolling(true);setWindow(initialWindow());}}>Live · rolling 24 h</button></div><TimeWindow value={window} timeZone={zone} onChange={w=>{setRolling(false);setWindow(w);}}/></div>
    {error&&<Notice>{error} Previous plots may be stale.</Notice>}
    {!data&&<p role="status">Loading the recent visibility and search plots…</p>}
    {data&&<><p className="muted">One long N–S reference baseline · {data.preset}. Native channels remain available in the baseline explorer.</p><div className="workflow-links"><Link to="/vis">Change baselines / processing</Link><Link to="/search">Explore T1 distributions</Link></div>
      <h3>T1 candidate distributions</h3>{data.t1?.error?<Notice>{data.t1.error}</Notice>:data.t1?.plot_url&&<figure className="plot-surface"><img src={data.t1.plot_url} alt="Rolling T1 gulp, beam, DM and width distributions"/><div className="plot-actions"><a download href={data.t1.plot_url}>Download PNG</a><Link to="/search">Coverage and cap-warning logs</Link></div><figcaption>{data.t1.note} DM display: 0–1000 pc cm⁻³.</figcaption></figure>}
      {[["phase_waterfall","Visibility phase"],["amplitude_waterfall","Dynamic spectrum"]].map(([kind,label])=><section key={kind}><h3>{label}</h3>{data[kind]?.error?<Notice>{data[kind].error}</Notice>:data[kind]&&<ProductPlots product={data[kind]}/>}</section>)}

    </>}
  </section>;
}
