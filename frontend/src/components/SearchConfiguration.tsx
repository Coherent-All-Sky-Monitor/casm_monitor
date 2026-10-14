import {useCachedResource} from './CachedResource';
import type {Json} from './Workspace';
import {maskSegments} from '../lib/searchMask';

const fmt=(v:number|null|undefined,digits=0)=>v==null?'—':v.toLocaleString('en-US',{maximumFractionDigits:digits});
const range=(lo:number|null,hi:number|null)=>lo==null||hi==null?'—':`${fmt(lo)}–${fmt(hi)}`;

export function SearchConfiguration(){
  const result=useCachedResource('/api/t1/configuration',30000,{allowStale:false,staleAfter:120000});
  const data=result.data;
  const fresh=!result.error&&data&&Date.now()-Date.parse(data.checked_utc)<120000;
  const streams:Json[]=data?.streams??[];
  const cfg:Json|null=fresh?data.common:null;
  const count=fresh?data.verified_count:0;
  const matched=fresh&&data.all_match;
  const status=!fresh?'Checking settings':matched?'All 8 match':count===8?'Settings differ':`${count}/8 verified`;
  const band=data?.band,mask=cfg?.mask,st=cfg?.sumthreshold;
  const segments=maskSegments(band,mask);
  const masked=segments?.filter(s=>s.masked).reduce((n,s)=>n+s.count,0);
  const percent=segments&&band?100*(masked??0)/band.nchan:null;
  const ticks=band?[band.low_mhz,...[400,420,440,460,480].filter(f=>f>band.low_mhz+.12*(band.high_mhz-band.low_mhz)&&f<band.high_mhz-.12*(band.high_mhz-band.low_mhz)),band.high_mhz]:[];
  return <section className="search-config" aria-label="Current HELLA search configuration">
    <header className="search-config-heading"><h3>Search configuration</h3><span className={`search-config-badge ${matched?'on':'unknown'}`}>{status}</span></header>
    {!cfg&&<p className="search-config-note" role="status">{result.error?'Configuration read unavailable.':!fresh?'Waiting for the latest configuration…':count===8?'Settings differ between streams. See stream details below.':'Some streams are unverified. See stream details below.'}</p>}
    <div className="search-config-body">
      <table className="search-settings-table" aria-label="Search settings"><thead><tr><th scope="col">Setting</th><th scope="col">Current value</th></tr></thead><tbody>
        <tr><th scope="row">DM range</th><td>{range(cfg?.dm_min,cfg?.dm_max)} <span>pc cm⁻³</span></td></tr>
        <tr><th scope="row">Search S/N</th><td>{cfg?.snr==null?'—':`≥ ${fmt(cfg.snr,2)}`}</td></tr>
        <tr><th scope="row">Boxcar widths</th><td>{cfg?.widths?.length?range(cfg.widths[0],cfg.widths[cfg.widths.length-1]):'—'} <span>samples · {cfg?.widths?.length??'—'} trials</span></td></tr>
        <tr><th scope="row">Gulp</th><td>{cfg?.gulp_s==null?'—':`${fmt(cfg.gulp_s,2)} s`} <span>· {fmt(cfg?.gulp)} samples</span></td></tr>
        <tr><th scope="row">SumThreshold RFI</th><td><span className={`search-config-badge ${st?.enabled===true?'on':st?.enabled===false?'off':'unknown'}`}>{st?.enabled===true?'ON':st?.enabled===false?'OFF':'Unknown'}</span></td></tr>
        <tr><th scope="row">Static channel mask</th><td>{segments?(masked?`${fmt(masked)} channels`:'OFF'):'Unknown'} {masked?<span>· {fmt(percent,1)}% of band</span>:null}</td></tr>
      </tbody></table>
      <section className="search-mask" aria-label="Static channel mask">
        <header><h4>Channel mask</h4><span>Low → high frequency</span></header>
        <div className="search-mask-track" role="img" aria-label={segments?segments.map(s=>`${s.masked?'Masked':'Kept'}: ${fmt(s.low,2)}–${fmt(s.high,2)} MHz, channels ${s.first}–${s.last}`).join('; '):'Channel mask unknown'}>
          {segments?segments.map(s=><div key={s.first} className={s.masked?'masked':'kept'} style={{width:`${100*s.fraction}%`}} title={`${s.masked?'Masked':'Kept'} · channels ${s.first}–${s.last} · ${fmt(s.low,2)}–${fmt(s.high,2)} MHz`}>{s.fraction>.13?(s.masked?'Masked':'Kept'):null}</div>):<span>Unknown</span>}
        </div>
        <div className="search-mask-axis" aria-hidden="true">{ticks.map((f:number,i:number)=><span key={f} style={{left:`${100*(f-band.low_mhz)/(band.high_mhz-band.low_mhz)}%`}} className={i===0?'first':i===ticks.length-1?'last':''}>{fmt(f,1)}</span>)}</div>
        <div className="search-mask-unit">Frequency (MHz)</div>
        <table className="search-mask-ranges" aria-label="Mask frequency ranges"><thead><tr><th scope="col">Band</th><th scope="col">Frequency (MHz)</th><th scope="col">Channels</th></tr></thead><tbody>
          {segments?segments.map(s=><tr key={s.first}><th scope="row"><i className={s.masked?'masked':'kept'}/>{s.masked?'Masked':'Kept'}</th><td>{fmt(s.low,2)}–{fmt(s.high,2)}</td><td>{s.first}–{s.last}</td></tr>):<tr><td colSpan={3}>Mask unavailable</td></tr>}
        </tbody></table>
        <p>{segments&&!masked?'No static mask. ':''}Channel 0 is the high-frequency end.{st?.enabled?' SumThreshold’s time-varying flags are separate.':''}</p>
      </section>
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
      <div className="search-config-table"><table><thead><tr><th>Stream</th><th>State</th><th>DM</th><th>S/N</th><th>SumThreshold</th><th>Mask channels</th></tr></thead><tbody>{streams.map(s=><tr key={s.stream}><th>{s.stream} · {s.node}</th><td>{fresh?s.status:'unknown'}</td><td>{fresh?range(s.settings?.dm_min,s.settings?.dm_max):'—'}</td><td>{fresh?fmt(s.settings?.snr,2):'—'}</td><td>{!fresh||!s.settings?'Unknown':s.settings.sumthreshold.enabled?'ON':'OFF'}</td><td>{!fresh||!s.settings||s.settings.mask.enabled==null?'Unknown':s.settings.mask.enabled?`${s.settings.mask.first}–${s.settings.mask.last}`:'OFF'}</td></tr>)}</tbody></table></div>
      <p>Current process-associated settings; independent of the plotted interval. Missing settings stay unknown.</p>
      {data?.nodes?.map((n:Json)=><p key={n.node}>{n.node} · checked {n.checked_utc?.replace('T',' ').replace(/\.\d+Z$/,' UTC').replace('Z',' UTC')??'not yet'}{n.error?` · ${n.error}`:''}</p>)}
    </details>
  </section>;
}
