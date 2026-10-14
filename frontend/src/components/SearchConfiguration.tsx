import {useState} from 'react';
import {useCachedResource} from './CachedResource';
import type {Json} from './Workspace';

const fmt=(v:number|null|undefined,digits=0)=>v==null?'—':v.toLocaleString('en-US',{maximumFractionDigits:digits});
const range=(lo:number|null,hi:number|null)=>lo==null||hi==null?'—':`${fmt(lo)}–${fmt(hi)}`;

export function SearchConfiguration(){
  const result=useCachedResource('/api/t1/configuration',30000,{allowStale:false,staleAfter:120000});
  const [selected,setSelected]=useState<number|null>(null);
  const data=result.data;
  const fresh=!result.error&&data&&Date.now()-Date.parse(data.checked_utc)<120000;
  const streams:Json[]=data?.streams??[];
  const row=streams.find(s=>s.stream===selected);
  const cfg:Json|null=fresh?(selected==null?data.common:row?.settings):null;
  const count=fresh?data.verified_count:0;
  const matched=fresh&&data.all_match;
  const status=!fresh?'Checking settings':matched?'All 8 match':count===8?'Settings differ':`${count}/8 verified`;
  const band=data?.band,mask=cfg?.mask,st=cfg?.sumthreshold;
  const maskWidth=mask?.enabled?100*mask.channels/(band?.nchan??3072):0;
  const maskLeft=mask?.enabled?100*((band?.nchan??3072)-1-mask.last)/(band?.nchan??3072):0;
  const maskKnown=cfg&&typeof mask?.enabled==='boolean';
  return <section className="search-config" aria-label="Current HELLA search configuration">
    <header className="search-config-heading"><div><span className="search-config-eyebrow">HELLA · CURRENT</span><h3>Search configuration</h3></div><span className={`search-config-badge ${matched?'on':'unknown'}`}>{status}</span></header>
    <div className="search-config-streams" role="group" aria-label="Configuration by stream">
      <button aria-pressed={selected==null} onClick={()=>setSelected(null)}>All streams</button>
      {Array.from({length:8},(_,i)=>{const s=streams.find(r=>r.stream===i);return <button key={i} aria-pressed={selected===i} className={fresh&&s?.status==='running'?'verified':''} title={`${i<4?'corr1':'corr2'} · ${fresh?(s?.reason??'Running process configuration'):'Unknown'}`} onClick={()=>setSelected(i)}>{i}</button>;})}
      <span>{selected==null?'corr1 + corr2':`Stream ${selected} · ${selected<4?'corr1':'corr2'}`}</span>
    </div>
    {!cfg&&<p className="search-config-note" role="status">{result.error?'Configuration read unavailable.':!fresh?'Waiting for the latest configuration…':selected!=null?(row?.reason??'Settings unavailable'):count===8?'Settings differ. Select a stream to inspect its configuration.':'Some streams are unverified. Select a stream for details.'}</p>}
    <div className="search-config-metrics">
      <div><span>DM range</span><strong>{range(cfg?.dm_min,cfg?.dm_max)}</strong><small>pc cm⁻³</small></div>
      <div><span>Search S/N</span><strong>{cfg?.snr==null?'—':`≥ ${fmt(cfg.snr,2)}`}</strong><small>T1 threshold</small></div>
      <div><span>Boxcar widths</span><strong>{cfg?.widths?.length?range(cfg.widths[0],cfg.widths[cfg.widths.length-1]):'—'}</strong><small>samples · {cfg?.widths?.length??'—'} trials</small></div>
      <div><span>Gulp</span><strong>{cfg?.gulp_s==null?'—':`${fmt(cfg.gulp_s,2)} s`}</strong><small>{fmt(cfg?.gulp)} samples</small></div>
      <div><span>SumThreshold RFI</span><strong><span className={`search-config-badge ${st?.enabled===true?'on':st?.enabled===false?'off':'unknown'}`}>{st?.enabled===true?'ON':st?.enabled===false?'OFF':'Unknown'}</span></strong><small>Channel-neighbour flagger</small></div>
      <div><span>Static channel mask</span><strong>{mask?.enabled===true?fmt(mask.channels):mask?.enabled===false?'OFF':'—'}</strong><small>{mask?.enabled?`channels ${mask.first}–${mask.last}`:'channels masked'}</small></div>
    </div>
    <div className="search-config-band">
      <div><span>{band?`${fmt(band.low_mhz,1)} MHz`:'—'}</span><span>{maskKnown?(mask?.enabled?`Masked ${fmt(mask.freq_low_mhz,2)}–${fmt(mask.freq_high_mhz,2)} MHz`:'No static channel mask'):'Mask unknown'}</span><span>{band?`${fmt(band.high_mhz,1)} MHz`:'—'}</span></div>
      <div className={`search-band-track ${maskKnown?'known':''}`} role="img" aria-label={maskKnown?(mask?.enabled?`Static mask, channels ${mask.first} through ${mask.last}, ${fmt(mask.freq_low_mhz,2)} to ${fmt(mask.freq_high_mhz,2)} MHz`:'No static channel mask'):'Channel mask unknown'}>{cfg&&mask?.enabled&&<i style={{left:`${maskLeft}%`,width:`${maskWidth}%`}}/>}</div>
    </div>
    <details className="search-config-details"><summary>Flagger settings & stream details</summary>
      {cfg&&<dl>
        <div><dt>SumThreshold</dt><dd>{st?.enabled?'ON · '+(st.parameters||'build defaults'):'OFF'}</dd></div>
        {st?.enabled&&<div><dt>Parameter order</dt><dd>χ₁ · ρ · min/max window (samples) · neighbour radius · optional blank fraction</dd></div>}
        <div><dt>Bandpass clipping</dt><dd>SPEC_MIN {fmt(cfg.spec_min,3)} · SPEC_MAX {fmt(cfg.spec_max,3)}</dd></div>
        <div><dt>SCRUNCH flagging</dt><dd>{cfg.scrunches.length?cfg.scrunches.join(' ; '):'No configured passes'}<small>time factor · frequency factor · threshold · iterations</small></dd></div>
        <div><dt>Native sampling</dt><dd>{fmt(band?.tsamp_ms,6)} ms · {fmt(band?.nchan)} channels · {fmt(cfg.nbeam)} beams/stream</dd></div>
        <div><dt>Boxcar trials</dt><dd>{cfg.widths.join(', ')||'Unknown'} samples<small>WIDTH_MIN {fmt(cfg.width_min)} · WIDTH_MAX {fmt(cfg.width_max)} · WIDTH_RATIO {fmt(cfg.width_ratio,2)}; largest trial ≤ WIDTH_MAX/2</small></dd></div>
        {Object.entries(cfg.parameters??{}).filter(([key])=>['BASELINE_WIDTH','BASELINE_MODE','BASELINE_SMOOTHER','U8_CLIP','CLUSTER_MINPTS','DM_TOL','DM_PULSE_WIDTH'].includes(key)).map(([key,value])=><div key={key}><dt>{key}</dt><dd>{String(value)}</dd></div>)}
      </dl>}
      <div className="search-config-table"><table><thead><tr><th>Stream</th><th>State</th><th>DM</th><th>S/N</th><th>SumThreshold</th><th>Mask channels</th></tr></thead><tbody>{streams.map(s=><tr key={s.stream}><th>{s.stream} · {s.node}</th><td>{fresh?s.status:'unknown'}</td><td>{fresh?range(s.settings?.dm_min,s.settings?.dm_max):'—'}</td><td>{fresh?fmt(s.settings?.snr,2):'—'}</td><td>{!fresh||!s.settings?'Unknown':s.settings.sumthreshold.enabled?'ON':'OFF'}</td><td>{!fresh||!s.settings?'—':s.settings.mask.enabled?`${s.settings.mask.first}–${s.settings.mask.last}`:'OFF'}</td></tr>)}</tbody></table></div>
      {selected!=null&&row&&<p>{row.config_path??'No configuration'}{row.pid?` · PID ${row.pid}`:''}{row.reason?` · ${row.reason}`:''}</p>}
      <p>Current process-associated settings; independent of the plotted interval. Missing settings stay unknown.</p>
      {data?.nodes?.map((n:Json)=><p key={n.node}>{n.node} · checked {n.checked_utc?.replace('T',' ').replace(/\.\d+Z$/,' UTC').replace('Z',' UTC')??'not yet'}{n.error?` · ${n.error}`:''}</p>)}
    </details>
  </section>;
}
