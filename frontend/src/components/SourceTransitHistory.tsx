import { useEffect, useState } from "react";
import { FigureZoom } from "./FigureZoom";
import { BeamLightCurve, LightCurve } from "./BeamLightCurve";
import { Json, LOCAL, Notice } from "./Workspace";
import { DynamicAxes, DynamicSpectrum, Tile } from "./vis/ArrayPlots";
import { cachedTransit } from './transitCache';
import "../array-vis.css";
import "../array-vis-compact.css";

type Beam = DynamicAxes & {tile:Tile; name:string; date:string; transit_unix:number; partial:boolean;
  calibration:{id:string;name:string}; samples:number; antenna_ids:number[]; details:Json; light_curve:LightCurve};

function BeamFigure({beam}:{beam:Beam}) {
  return <><DynamicSpectrum tile={beam.tile} data={beam} zone={LOCAL} quantity="real"
    valueLabel="Beam cross-power (weighted counts)" marker={{time:beam.transit_unix,label:'Transit'}}/>
    <BeamLightCurve curve={beam.light_curve} t0={beam.t0} t1={beam.t1} integration={beam.integration_s} transit={beam.transit_unix} zone={LOCAL}/></>;
}

export function SourceTransitHistory({data}:{data:Json}) {
  const [beams,setBeams] = useState<Record<string,Beam>>({});
  const [errors,setErrors] = useState<Record<string,string>>({});
  const [focus,setFocus] = useState<Beam|null>(null);
  const [retry,setRetry] = useState(0);
  const cal = data.calibration;
  const dates:Json[] = data.transits ?? [];
  useEffect(() => {
    let active = true;
    if (!cal) return;
    setErrors({});
    setBeams(old=>Object.fromEntries(dates.filter(row=>old[row.cache_key]).map(row=>[row.cache_key,old[row.cache_key]])));
    const load = async () => {
      for (const row of dates) {
        if (!active) break;
        try {
          const body = await cachedTransit(data.source,row,cal.id) as Beam;
          if (active) setBeams(old => ({...old,[row.cache_key]:body}));
        } catch (e) {
          if (active) setErrors(old => ({...old,[row.date]:(e as Error).message}));
        }
      }
    };
    void load();
    return () => {active=false;};
  },[data,retry]);
  const dateLabel = (t:number) => new Intl.DateTimeFormat('en-CA',{timeZone:LOCAL,year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(t*1000));
  return <section className="source-transits">
    <p className="history-caption muted">{data.name} · Fixed beam at transit · ±{data.window_hours/2} hours</p>
    {cal?<p className="history-cal muted">Deployed cal: <span title={cal.path}>{cal.name}</span> · used for every date</p>:<Notice>Deployed calibration unavailable.</Notice>}
    <div className="history-grid">{dates.map(row => {
      const beam = beams[row.cache_key];
      return <article className="history-entry transit-entry" key={row.date}>
        <header><h3>{dateLabel(row.transit_unix)}</h3><small>OVRO local</small></header>
        {beam?<>
          <div className="plot-zoom-trigger" role="button" tabIndex={0} aria-label={`Enlarge ${data.name} transit ${dateLabel(row.transit_unix)}`}
            onClick={()=>setFocus(beam)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();setFocus(beam);}}}><BeamFigure beam={beam}/></div>
          {beam.partial&&<p className="muted">Partial time coverage</p>}
          <details className="history-notes"><summary>Plot details</summary>
            <p>{beam.antenna_ids.length} antennas · {beam.samples} integrations · {beam.calibration.name}</p>
            <p>Cross-power only; autos excluded. Negative values are retained. No background subtraction or flux calibration.</p>
            <p>Fixed pointing at transit. Tests visibility phasing with the current calibration, not the quantized weights uploaded to hardware. RFI and other sources can affect the curve.</p>
            <p>Today’s calibration is applied to this date; it need not be the calibration used then.</p>
            <pre>{JSON.stringify(beam.details,null,2)}</pre>
          </details>
        </>:!cal?<p className="empty-plot">Calibration unavailable</p>:errors[row.date]?<Notice>{errors[row.date]}</Notice>:<p className="empty-plot" role="status">Loading beam…</p>}
      </article>;
    })}</div>
    {!dates.length&&<Notice>No cached native visibilities around this source’s transits.</Notice>}
    {!!Object.keys(errors).length&&<button onClick={()=>setRetry(v=>v+1)}>Retry beam plots</button>}
    <p className="muted history-caption">Latest {data.max_transits??3} completed transits · saved in this browser</p>
    {focus&&<FigureZoom title={`${focus.name} · ${dateLabel(focus.transit_unix)} · fixed transit beam`} onClose={()=>setFocus(null)}
      actions={<p>Cal: {focus.calibration.name} · {focus.antenna_ids.length} antennas · cross-power only</p>}><BeamFigure beam={focus}/></FigureZoom>}
  </section>;
}
