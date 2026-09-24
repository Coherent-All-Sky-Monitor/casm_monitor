import { useEffect, useState } from "react";
import { FigureZoom } from "./FigureZoom";
import { Json, LOCAL, Notice } from "./Workspace";
import { DynamicAxes, DynamicSpectrum, Tile } from "./vis/ArrayPlots";
import "../array-vis.css";
import "../array-vis-compact.css";

type Beam = DynamicAxes & {tile:Tile; name:string; date:string; transit_unix:number; partial:boolean;
  calibration:{id:string;name:string}; samples:number; antenna_ids:number[]; details:Json};

function BeamFigure({beam}:{beam:Beam}) {
  return <DynamicSpectrum tile={beam.tile} data={beam} zone={LOCAL} quantity="real"
    valueLabel="Beam cross-power (weighted counts)" marker={{time:beam.transit_unix,label:'Transit'}}/>;
}

export function SourceTransitHistory({data}:{data:Json}) {
  const [beams,setBeams] = useState<Record<string,Beam>>({});
  const [errors,setErrors] = useState<Record<string,string>>({});
  const [focus,setFocus] = useState<Beam|null>(null);
  const [retry,setRetry] = useState(0);
  const cal = data.calibration;
  const dates:Json[] = data.transits ?? [];
  useEffect(() => {
    const controller = new AbortController();
    if (!cal) return;
    setErrors({});
    // One native selected-triangle read at a time; abort outstanding browser
    // requests when switching source. The server serializes large reads too.
    const load = async () => {
      for (const row of dates) {
        if (controller.signal.aborted) break;
        try {
          const response = await fetch(`/api/sources/transits/${data.source}/${row.date}?calibration_id=${cal.id}`, {signal:controller.signal});
          if (!response.ok) {
            const error = await response.json().catch(()=>null);
            throw new Error(error?.detail ?? `Beam unavailable (HTTP ${response.status})`);
          }
          const body = await response.json();
          if (!controller.signal.aborted) setBeams(old => ({...old,[row.date]:body}));
        } catch (e) {
          if (!controller.signal.aborted) setErrors(old => ({...old,[row.date]:(e as Error).message}));
        }
      }
    };
    void load();
    return () => controller.abort();
  },[data,retry]);
  const dateLabel = (t:number) => new Intl.DateTimeFormat('en-CA',{timeZone:LOCAL,year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(t*1000));
  return <section className="source-transits">
    <p className="history-caption muted">{data.name} · Source-tracking beam · ±1 hour around transit</p>
    {cal?<p className="history-cal muted">Current cal: <span title={cal.path}>{cal.name}</span> · used for every date</p>:<Notice>Current calibration unavailable.</Notice>}
    <div className="history-grid">{dates.map(row => {
      const beam = beams[row.date];
      return <article className="history-entry transit-entry" key={row.date}>
        <header><h3>{dateLabel(row.transit_unix)}</h3><small>OVRO local</small></header>
        {beam?<>
          <div className="plot-zoom-trigger" role="button" tabIndex={0} aria-label={`Enlarge ${data.name} transit ${dateLabel(row.transit_unix)}`}
            onClick={()=>setFocus(beam)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();setFocus(beam);}}}><BeamFigure beam={beam}/></div>
          {beam.partial&&<p className="muted">Partial time coverage</p>}
          <details className="history-notes"><summary>Plot details</summary>
            <p>{beam.antenna_ids.length} antennas · {beam.samples} integrations · {beam.calibration.name}</p>
            <p>Cross-power only; autos excluded. Negative values are retained. No background subtraction or flux calibration.</p>
            <p>Today’s calibration is applied to this date; it need not be the calibration used then.</p>
            <pre>{JSON.stringify(beam.details,null,2)}</pre>
          </details>
        </>:!cal?<p className="empty-plot">Calibration unavailable</p>:errors[row.date]?<Notice>{errors[row.date]}</Notice>:<p className="empty-plot" role="status">Loading beam…</p>}
      </article>;
    })}</div>
    {!dates.length&&<Notice>No cached native visibilities around this source’s transits.</Notice>}
    {!!Object.keys(errors).length&&<button onClick={()=>setRetry(v=>v+1)}>Retry beam plots</button>}
    <p className="muted history-caption">Available native-cache history (normally 3 days). Older averaged visibilities are not substituted.</p>
    {focus&&<FigureZoom title={`${focus.name} · ${dateLabel(focus.transit_unix)} · tracking beam`} onClose={()=>setFocus(null)}
      actions={<p>Cal: {focus.calibration.name} · {focus.antenna_ids.length} antennas · cross-power only</p>}><BeamFigure beam={focus}/></FigureZoom>}
  </section>;
}
