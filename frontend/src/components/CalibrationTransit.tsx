import { useState } from 'react';
import { Json, Notice, ProductImage, useResource } from './Workspace';

const SOURCES: Record<string,string> = {'sun':'Sun', 'cyg-a':'Cyg A', 'cas-a':'Cas A', 'tau-a':'Tau A'};
const timeLabel = (value:string, local:boolean) => new Date(value).toLocaleString('en-US', {
  timeZone:local?'America/Los_Angeles':'UTC', month:'short', day:'numeric',
  hour:'2-digit', minute:'2-digit', hour12:false, timeZoneName:'short',
});

export default function CalibrationTransit({tag, action}: {tag:string; action:Json}) {
  const [scale, setScale] = useState('mean');
  const [local, setLocal] = useState(true);
  const [amplitude, setAmplitude] = useState('amp-uniform');
  const [timingOpen, setTimingOpen] = useState(false);
  const root = `/api/calibration/builds/${tag}/transit/${action.action_id}`;
  const {data, error} = useResource(`${root}?scale=${scale}`);
  const current = data?.scale === scale ? data : null;
  const calibrations:Json[] = current?.items.filter((item:Json)=>item.kind==='calibration') ?? [];
  const amplitudes:Json[] = current?.items.filter((item:Json)=>item.kind==='amplitude') ?? [];
  const selected = amplitudes.find(item=>item.id===amplitude) ?? amplitudes[0];
  const plot = (kind:string, item?:Json) => `${root}/${kind}.png?scale=${scale}${item?`&series=${encodeURIComponent(item.id)}`:''}`;
  const figure = (kind:string, label:string, item?:Json) => <figure key={`${kind}-${item?.id ?? ''}-${scale}`}>
    <ProductImage url={plot(kind,item)} label={label}/>
    <figcaption><span>{label}</span><a href={plot(kind,item)} download>Download PNG</a></figcaption>
  </figure>;
  return <section className="cal-transit-review" aria-label="Saved transit validation">
    <div className="cal-section-heading"><h3>{SOURCES[action.selection.source]??action.selection.source} · {action.selection.date}</h3><span>Saved transit · click any plot to zoom</span></div>
    <div className="cal-transit-toolbar">
      <div className="segmented" role="group" aria-label="Transit power scale">
        {[['mean','Weighted mean'],['sum','Raw sum']].map(([value,label])=><button key={value} type="button" className={scale===value?'selected':''} aria-pressed={scale===value} onClick={()=>setScale(value)}>{label}</button>)}
      </div>
      <p className="cal-caption">{scale==='mean'?'Cross-power divided by the total baseline amplitude weight in each channel. Receiver gains remain unequal; counts are not calibrated flux.':'Signed cross-power sum. Its size depends on weight amplitudes; curve height alone does not measure sensitivity.'}</p>
    </div>
    {error?<Notice>{error}</Notice>:!current?<p className="cal-caption">Preparing saved transit plots…</p>:<>
      <section className="cal-card cal-transit-background">
        <div className="cal-section-heading"><h3>Background windows</h3><div className="segmented" role="group" aria-label="Background time zone">
          <button type="button" className={local?'selected':''} onClick={()=>setLocal(true)}>OVRO local</button>
          <button type="button" className={!local?'selected':''} onClick={()=>setLocal(false)}>UTC</button>
        </div></div>
        <p>{current.background.policy==='dark'?'Sun ≤ −10° · ':current.background.policy==='outer_window'?'Solar self-check · ':'Solar-contamination warning · '}{current.background.samples} integrations · {current.background.before_samples} before / {current.background.after_samples} after</p>
        {current.background.excluded_sun_up>0&&<p className="cal-transit-warning">Excluded {current.background.excluded_sun_up} Sun-up integrations from the background.</p>}
        {current.background.warning&&<p className="cal-transit-warning">{current.background.warning}</p>}
        {current.background.intervals.map((interval:Json,index:number)=><p className="cal-caption" key={index}>
          {timeLabel(interval.utc[0],local)} – {timeLabel(interval.utc[1],local)} · Sun {interval.sun_alt_min_deg.toFixed(1)}° to {interval.sun_alt_max_deg.toFixed(1)}°
        </p>)}
        <p className="cal-caption">Background samples are beyond ±{current.background.off_minutes} min from transit. Solar altitude is checked at each sample time. This background is separate from the night template used in the solve.</p>
      </section>
      {!current.background.valid?<Notice>{current.background.reason}</Notice>:<>
        <p className="cal-caption">Excess / background std = (power − background mean) / background standard deviation. It includes sky variations and interference. A peak alone does not identify the source.</p>
        {current.notices.map((notice:string)=><Notice key={notice}>{notice}</Notice>)}
        <div className="cal-plot-grid single">
          {calibrations.length>1&&calibrations.every(item=>item.available)&&figure('calibration','Calibration comparison')}
          {calibrations.filter(item=>item.available).map(item=>figure('transit',item.label,item))}
        </div>
        {amplitudes.length>0&&<section className="cal-gallery"><div className="cal-section-heading"><h3>Amplitude schemes</h3><span>Same antennas and channels</span></div>
          <div className="cal-plot-grid single">{amplitudes.every(item=>item.available)&&figure('amplitude','Amplitude scheme comparison')}</div>
          <div className="segmented" role="group" aria-label="Amplitude transit spectrum">{amplitudes.map(item=><button key={item.id} type="button" className={selected?.id===item.id?'selected':''} aria-pressed={selected?.id===item.id} onClick={()=>setAmplitude(item.id)}>{item.label}</button>)}</div>
          <div className="cal-plot-grid single">{selected?.available&&figure('transit',`${selected.label} · dynamic spectrum and power curve`,selected)}</div>
        </section>}
        <details className="cal-card" onToggle={event=>setTimingOpen(event.currentTarget.open)}><summary>Optional diagnostic · Peak time vs frequency</summary>
          <p className="cal-caption">Each dot is the strongest smoothed feature in one channel anywhere in the five-hour window. It may be another sky feature or interference. The orange line is the ideal array prediction; weak channels and edge peaks are omitted.</p>
          {timingOpen&&<div className="cal-plot-grid single">{calibrations.filter(item=>item.available&&item.metrics).map(item=>figure('timing',`${item.label} · peak time vs frequency`,item))}</div>}
        </details>
      </>}
    </>}
  </section>;
}
