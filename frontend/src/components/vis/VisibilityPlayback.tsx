import {CSSProperties,memo,useEffect,useMemo,useRef,useState} from 'react';
import {createPortal} from 'react-dom';
import {FigureZoom} from '../FigureZoom';
import {SpectrumClocks} from '../SpectrumClocks';
import {Notice} from '../Workspace';
import {playbackColumns,playbackNext,playbackStamp,PlaybackTimeZone} from '../snapPlaybackModel';
import {Antenna,LayoutMap,location,QUANTITIES,Quantity,Spectrum,wiringLabel} from './ArrayPlots';
import {chunkValue,Encoding,lowerBound,MovieManifest,movieOrder,RollingSpectra,VISIBILITY_SPEEDS,visibilityCompactColumns,visibilityDuration,windowFirst} from './visibilityPlaybackModel';
import {MovieBuffer,useVisibilityFrames} from './useVisibilityFrames';
import {VisibilityDynamic} from './VisibilityDynamic';
import '../../snap-playback.css';
import '../../visibility-playback.css';

export type MovieOptions={mode:string;view:string;quantity:Quantity;reference:string;referenceInput:number;arrangement:string;windowHours:number;statistic:'latest'|'mean';log:boolean;zone:PlaybackTimeZone;band:number[];selected:number[];end:number};
type Expanded={pair:number;a:Antenna;b:Antenna;conjugate:boolean};
const MemoSpectrum=memo(Spectrum);

function useManifest(query:string,cache:Map<string,MovieManifest>,attempt:number){
  const [result,setResult]=useState<{query:string;data:MovieManifest}|null>(null),[error,setError]=useState('');
  useEffect(()=>{
    const cached=cache.get(query);setError('');if(cached){setResult({query,data:cached});return;}
    const controller=new AbortController();
    fetch('/api/science/array/playback?'+query,{signal:controller.signal}).then(async r=>{
      const d=await r.json();if(!r.ok)throw new Error(d.detail??`History unavailable (${r.status})`);
      if(d.version!==1||!d.times?.length||d.first_frame>=d.times.length)throw new Error('Empty visibility sequence');
      if(!controller.signal.aborted){cache.set(query,d);while(cache.size>8)cache.delete(cache.keys().next().value!);setResult({query,data:d});}
    }).catch(e=>{if(!controller.signal.aborted)setError(e.message);});
    return()=>controller.abort();
  },[query,cache,attempt]);
  return {data:result?.query===query?result.data:null,error};
}

export function VisibilityPlayback({initial,inputs,onClose,onInspect}:{initial:MovieOptions;inputs:Antenna[];onClose:()=>void;onInspect:(pair:number[],t0:number,t1:number,options:MovieOptions)=>void}){
  const dialog=useRef<HTMLDialogElement>(null);
  const [options,setOptions]=useState(initial),[span,setSpan]=useState(1),[cursor,setCursor]=useState<number|null>(null);
  const [speed,setSpeed]=useState(1),[loop,setLoop]=useState(false),[playing,setPlaying]=useState(false),[expanded,setExpanded]=useState<Expanded|null>(null);
  const [attempt,setAttempt]=useState(0),[size,setSize]=useState({width:1200,height:700});
  const cache=useMemo(()=>new MovieBuffer(),[]),manifests=useMemo(()=>new Map<string,MovieManifest>(),[]);
  const stage=useRef<HTMLDivElement>(null),deadline=useRef<number|null>(null),previousSpeed=useRef(speed);
  const mode=options.view==='matrix'?'matrix':options.mode;
  const query=new URLSearchParams({inputs:options.selected.join(','),mode,reference_input:String(mode==='matrix'?0:options.referenceInput),reference:options.reference,
    span_days:String(span),window_hours:String(options.windowHours),end:String(options.end),fmin:String(options.band[0]),fmax:String(options.band[1])}).toString();
  const manifest=useManifest(query,manifests,attempt),m=manifest.data;
  const index=m?Math.max(m.first_frame,Math.min(m.times.length-1,lowerBound(m.times,cursor??m.times[m.first_frame]))):0;
  const encoding:Encoding=options.view==='spectrum'?'complex':'preview',windowed=options.view!=='spectrum'||options.statistic==='mean';
  const buffer=useVisibilityFrames(m,index,encoding,windowed,loop,cache);
  const ready=!!m&&buffer.ready;
  const first=m?windowFirst(m,index):0;
  const chunks=useMemo(()=>{
    if(!m||!ready)return [];
    const out=[];for(let b=Math.floor((windowed?first:index)/m.chunk_size)*m.chunk_size;b<=index;b+=m.chunk_size){const c=cache.get(m,b,encoding);if(c)out.push(c);}return out;
  },[m,ready,first,index,windowed,encoding,cache]);
  const rolling=useMemo(()=>m?new RollingSpectra(m.pairs.length,m.freq_mhz.length):null,[m]);
  const spectra=useMemo(()=>{
    if(!m||!ready||encoding!=='complex')return [];
    if(options.statistic==='mean'){
      rolling!.update(first,index,i=>cache.get(m,i,'complex'));
      return m.pairs.map((_,p)=>rolling!.spectrum(p,options.quantity));
    }
    const c=cache.get(m,index,'complex')!;
    return m.pairs.map((_,p)=>m.freq_mhz.map((_,ch)=>chunkValue(c,index,p,ch,options.quantity)));
  },[m,ready,index,first,encoding,options.statistic,options.quantity,rolling,cache]);
  const change=(delta:Partial<MovieOptions>)=>{setPlaying(false);setExpanded(null);if(m)setCursor(m.times[index]);setOptions(o=>({...o,...delta}));};
  const seek=(i:number)=>{if(m){setPlaying(false);setCursor(m.times[i]);}};
  const toggle=()=>{if(playing)setPlaying(false);else{if(m&&index===m.times.length-1)setCursor(m.times[m.first_frame]);setPlaying(true);}};
  useEffect(()=>{
    const previous=document.activeElement as HTMLElement|null,el=dialog.current!;el.showModal();
    const overflow=document.body.style.overflow;document.body.style.overflow='hidden';
    return()=>{el.close();document.body.style.overflow=overflow;previous?.focus();};
  },[]);
  useEffect(()=>{
    const measure=()=>{const el=stage.current;if(el?.clientWidth&&el.clientHeight)setSize({width:el.clientWidth,height:el.clientHeight});};
    measure();const timer=window.setTimeout(measure,0),observer=new ResizeObserver(measure);observer.observe(stage.current!);
    return()=>{observer.disconnect();window.clearTimeout(timer);};
  },[]);
  useEffect(()=>{if(manifest.error||buffer.error)setPlaying(false);},[manifest.error,buffer.error]);
  useEffect(()=>{
    const hide=()=>{if(document.hidden)setPlaying(false);};document.addEventListener('visibilitychange',hide);return()=>document.removeEventListener('visibilitychange',hide);
  },[]);
  useEffect(()=>{
    if(!playing||!ready||!m||document.hidden){deadline.current=null;return;}
    if(previousSpeed.current!==speed){deadline.current=null;previousSpeed.current=speed;}
    const period=1000/speed;if(deadline.current===null)deadline.current=performance.now()+period;
    const timer=window.setTimeout(()=>{
      deadline.current=performance.now()+period;
      const next=playbackNext(index-m.first_frame,m.times.length-m.first_frame,loop);
      if(next===null)setPlaying(false);else setCursor(m.times[m.first_frame+next]);
    },Math.max(0,deadline.current-performance.now()));return()=>window.clearTimeout(timer);
  },[playing,ready,m,index,speed,loop,expanded]);
  const antennas=m?.inputs??inputs,ordered=movieOrder(antennas,options.selected,options.arrangement);
  const ref=antennas.find(a=>a.packet_idx===options.referenceInput);
  const compact=options.arrangement==='compact';
  const columns=compact?visibilityCompactColumns(ordered.length,size.width):playbackColumns(ordered.length||1,size.width,size.height),rows=Math.ceil(ordered.length/columns);
  const at=m?.times[index],label=QUANTITIES.find(q=>q[0]===options.quantity)![1];
  const day=new Date(options.end*1000).toISOString().slice(0,10);
  const error=manifest.error||buffer.error;
  const status=error?'Playback unavailable':!m?'Preparing saved sequence & fixed scales…':!ready?`Buffering ${buffer.loaded} / ${buffer.total} chunks…`:playing?'Playing':'Paused';
  const gap=m&&index>m.first_frame&&m.times[index]-m.times[index-1]>m.integration_s*1.5;
  const pairFor=(a:Antenna,b:Antenna):Expanded|null=>{
    if(!m)return null;let pair=m.pairs.findIndex(([i,j])=>i===a.packet_idx&&j===b.packet_idx);let conjugate=false;
    if(pair<0){pair=m.pairs.findIndex(([i,j])=>i===b.packet_idx&&j===a.packet_idx);conjugate=true;}
    return pair<0?null:{pair,a,b,conjugate};
  };
  const open=(p:Expanded)=>{setPlaying(false);setExpanded(p);};
  const closeExpanded=()=>{setPlaying(false);setExpanded(null);};
  const plot=(p:Expanded,mini=false)=>!m?null:options.view==='spectrum'
    ?<MemoSpectrum values={spectra[p.pair]??[]} freq={m.freq_mhz} quantity={options.quantity} log={options.log&&options.quantity==='amp'} range={m.limits[p.pair].spectrum[options.quantity==='amp'&&options.log?'amp_log':options.quantity]}/>
    :<VisibilityDynamic manifest={m} chunks={chunks} index={index} pair={p.pair} quantity={options.quantity} zone={options.zone} mini={mini} conjugate={p.conjugate}/>;
  const card=(a:Antenna)=>{
    const b=mode==='auto'?a:ref,p=b?pairFor(a,b):null;if(!p)return null;
    return <article className={`snap-movie-card vis-movie-card${a.beamforming?' in-beamforming':''}`} key={a.packet_idx} data-input={a.packet_idx}>
      <header><strong>{a.station}<small>ant {a.antenna}</small></strong><span>{a.packet_idx===b!.packet_idx?'auto':`× ${b!.station}`}<small>{wiringLabel(a)}</small></span></header>
      <button className="vis-movie-trigger" aria-label={`Enlarge playback ${a.station}`} onClick={()=>open(p)}>{plot(p)}</button>
      <footer>{label} · {options.view==='spectrum'?(options.statistic==='mean'?`${options.windowHours} h average`:'One integration'):`${options.windowHours} h window`}</footer>
    </article>;
  };
  const matrix=()=> <div className="vis-movie-matrix"><table><thead><tr><th>V(row, column)</th>{ordered.map(a=><th key={a.packet_idx} scope="col">{a.station}</th>)}</tr></thead><tbody>{ordered.map((a,i)=><tr key={a.packet_idx}><th scope="row">{a.station}</th>{ordered.map((b,j)=>{const p=j>=i?pairFor(a,b):null;return <td key={b.packet_idx}>{p&&<button aria-label={`Enlarge playback ${a.station} × ${b.station}`} onClick={()=>open(p)}>{plot(p,true)}{i===j&&<small>auto</small>}</button>}</td>;})}</tr>)}</tbody></table></div>;
  const physical=()=> <div className="vis-movie-stations">{[...new Set(antennas.map(a=>location(a).row))].sort((a,b)=>b-a).map(row=><section key={row}><h4>N{String(row).padStart(2,'0')}</h4><div>{[1,2,3,4,5,6].map(col=>{const a=antennas.find(a=>location(a).row===row&&location(a).col===col);return a&&options.selected.includes(a.packet_idx)?card(a):<div className="vis-movie-empty-station" key={col}>E{col}<span>{a?'Hidden':'× No antenna'}</span></div>;})}</div></section>)}</div>;
  const reopen=()=>{manifests.delete(query);setAttempt(n=>n+1);buffer.retry();};
  return createPortal(<dialog ref={dialog} className="snap-movie vis-movie" aria-label="Visibility history playback" onCancel={e=>{if(e.target===e.currentTarget){e.preventDefault();onClose();}}}>
    <section className="snap-movie-content">
      <header className="snap-movie-header"><div><h2>Visibility history</h2><span>{ordered.length} antennas · {options.reference==='raw'?'Raw':'Sun fringe-stopped'} · 8-channel averaged</span></div>
        <div className="snap-movie-clock">{m&&at!==undefined?<SpectrumClocks at={at} lst={m.lst[index]} dark/>:<strong>{day}</strong>}<small role="status">{status}{m?` · integration ${index-m.first_frame+1} / ${m.times.length-m.first_frame}`:''}</small></div>
        <button onClick={onClose} autoFocus aria-label="Close visibility playback">Close ×</button></header>
      <div className="snap-movie-controls">
        <button className="snap-movie-play" onClick={toggle} disabled={!m||!!error||(!ready&&!playing)} aria-label={playing?'Pause visibility playback':'Play visibility playback'}>{playing?'Pause':m&&index===m.times.length-1?'Replay':'Play'}</button>
        <div className="snap-movie-range" aria-label="Visibility playback period">{[[1,'24 hours'],[2,'48 hours'],[7,'Last week']].map(([v,l])=><button key={v} aria-pressed={span===v} onClick={()=>{setPlaying(false);setSpan(Number(v));setCursor(null);}}>{l}</button>)}</div>
        <label>Through · UTC<input type="date" aria-label="Visibility playback date (UTC)" value={day} min={m?new Date(m.availability.start*1000).toISOString().slice(0,10):undefined} max={new Date((m?.availability.end??initial.end)*1000).toISOString().slice(0,10)} onChange={e=>{if(e.target.value){setPlaying(false);setCursor(null);setOptions(o=>({...o,end:Math.min(initial.end,Date.parse(e.target.value+'T00:00:00Z')/1000+86400-.001)}));}}}/></label>
        <label>Speed<select aria-label="Visibility playback speed" title="Estimated full movie duration, excluding buffering. Every saved integration is retained." value={speed} onChange={e=>setSpeed(Number(e.target.value))}>{VISIBILITY_SPEEDS.map(s=><option key={s} value={s}>{s}×{m?` · ~${visibilityDuration(m.times.length-m.first_frame,s)}`:''}</option>)}</select></label>
        <label className="snap-movie-loop"><input type="checkbox" checked={loop} onChange={e=>setLoop(e.target.checked)}/>Loop</label><span className="snap-movie-cadence" title="Estimated at the selected speed; buffering or slow rendering adds time. 1× = 1 integration per second.">{m?`~${visibilityDuration(m.times.length-index,speed)} remaining`:'1× = 1 s per integration'}</span>
      </div>
      <div className="snap-movie-controls vis-movie-choices">
        <label>Correlation<select aria-label="Playback correlation" value={options.mode} onChange={e=>change({mode:e.target.value,view:e.target.value==='auto'?'spectrum':'dynamic'})}><option value="auto">Autocorrelations</option><option value="cross">Cross-correlations</option></select></label>
        {options.mode==='cross'&&options.view!=='matrix'&&<label>Reference<select aria-label="Playback reference antenna" value={options.referenceInput} onChange={e=>change({referenceInput:Number(e.target.value)})}>{antennas.map(a=><option key={a.packet_idx} value={a.packet_idx}>{a.station} · ant {a.antenna}</option>)}</select></label>}
        <div className="snap-movie-range" aria-label="Playback quantity">{QUANTITIES.map(([q,l])=><button key={q} aria-pressed={q===options.quantity} onClick={()=>change({quantity:q})}>{l}</button>)}</div>
        <label>View<select aria-label="Playback view" value={options.view} onChange={e=>change({view:e.target.value,...(e.target.value==='matrix'?{mode:'cross'}:{})})}><option value="spectrum">Spectrum</option><option value="dynamic">Dynamic spectrum</option><option value="matrix">All pairs</option></select></label>
        <label>Order<select aria-label="Playback arrangement" value={options.arrangement} onChange={e=>change({arrangement:e.target.value})}><option value="compact">Compact panels</option><option value="snap">SNAP order</option><option value="physical">Station grid</option></select></label>
        <details className="vis-movie-settings"><summary>Plot options & antennas</summary><div>
          <label>Panel window<select aria-label="Playback panel window" value={options.windowHours} onChange={e=>change({windowHours:Number(e.target.value)})}>{[[.5,'30 min'],[2,'2 h'],[6,'6 h'],[24,'24 h']].map(([v,l])=><option value={v} key={v}>{l}</option>)}</select></label>
          <label>Processing<select value={options.reference} onChange={e=>change({reference:e.target.value})}><option value="raw">Raw</option><option value="sun">Sun fringe-stopped</option></select></label>
          {options.view==='spectrum'&&<><label>Spectrum<select aria-label="Playback spectrum statistic" value={options.statistic} onChange={e=>change({statistic:e.target.value as 'latest'|'mean'})}><option value="latest">One integration</option><option value="mean">Window average</option></select></label><label>Amplitude scale<select value={options.log?'log':'linear'} onChange={e=>change({log:e.target.value==='log'})}><option value="log">Logarithmic</option><option value="linear">Linear</option></select></label></>}
          <label>Time labels<select aria-label="Visibility playback time zone" value={options.zone} onChange={e=>change({zone:e.target.value as PlaybackTimeZone})}><option value="America/Los_Angeles">OVRO local</option><option value="UTC">UTC</option></select></label>
          <p>Fixed scales per baseline. Spectra retain cached channels; dynamic previews average frequency to ≤128 channels. No time interpolation or channel normalization.</p>
          <LayoutMap inputs={antennas} selected={options.selected} reference={options.referenceInput} mode={options.mode} onToggle={id=>{const selected=options.selected.includes(id)?options.selected.filter(i=>i!==id):[...options.selected,id];if(selected.length)change({selected});}}/>
          <button onClick={()=>change({selected:antennas.filter(a=>a.beamforming).map(a=>a.packet_idx)})} disabled={!antennas.some(a=>a.beamforming)}>Beamforming</button><button onClick={()=>change({selected:antennas.map(a=>a.packet_idx)})}>All wired</button>
        </div></details>
      </div>
      <div className="snap-movie-timeline"><button aria-label="Previous visibility frame" disabled={!m||index<=m.first_frame} onClick={()=>seek(index-1)}>‹</button><input type="range" aria-label="Visibility saved time" min={m?.first_frame??0} max={m?m.times.length-1:0} value={index} disabled={!m} aria-valuetext={at===undefined?'No saved integration':playbackStamp(at,options.zone)} onChange={e=>seek(Number(e.target.value))}/><button aria-label="Next visibility frame" disabled={!m||index>=m.times.length-1} onClick={()=>seek(index+1)}>›</button><span>{gap?'Gap before this integration · ':''}{options.view==='spectrum'?(options.statistic==='mean'?`${options.windowHours} h average · mean |V|, phase of complex mean`:'One saved integration'):`${options.windowHours} h moving window · dark pixels: missing/undefined`}</span></div>
      {error&&<div className="snap-movie-error"><Notice>{error}</Notice><button onClick={reopen}>Reload saved sequence</button></div>}
      <div className="snap-movie-stage" ref={stage} aria-busy={!ready} data-frame={ready?index:undefined}>
        {ready&&!expanded?(options.view==='matrix'?matrix():options.arrangement==='physical'?physical():<div className="snap-movie-grid" style={{gridTemplateColumns:`repeat(${columns},minmax(0,1fr))`,gridTemplateRows:`repeat(${rows},minmax(${compact?240:170}px,1fr))`} as CSSProperties}>{ordered.map(card)}</div>):<div className="snap-movie-empty">{expanded?'Baseline open in zoom':status}</div>}
      </div>
      {expanded&&m&&<FigureZoom title={`${expanded.a.station} × ${expanded.b.station} · ${label} · ${playbackStamp(at!,options.zone)}`} onClose={closeExpanded} onBeforeDownload={()=>setPlaying(false)} downloadDisabled={!ready}
        controls={<div className="vis-zoom-playback" aria-label="Zoomed baseline playback">
          <div><button className="vis-zoom-play" onClick={toggle} disabled={!!error||(!ready&&!playing)} aria-label={playing?'Pause baseline playback':'Play baseline playback'}>{playing?'Pause':index===m.times.length-1?'Replay':'Play'}</button>
            <label>Speed<select aria-label="Zoomed baseline playback speed" value={speed} onChange={e=>setSpeed(Number(e.target.value))}>{VISIBILITY_SPEEDS.map(s=><option key={s} value={s}>{s}× · ~{visibilityDuration(m.times.length-m.first_frame,s)}</option>)}</select></label>
            <label><input type="checkbox" aria-label="Loop zoomed baseline" checked={loop} onChange={e=>setLoop(e.target.checked)}/>Loop</label>
            <span role="status">{status} · {index-m.first_frame+1} / {m.times.length-m.first_frame}</span></div>
          <div><button aria-label="Previous baseline frame" disabled={index<=m.first_frame} onClick={()=>seek(index-1)}>‹</button>
            <input type="range" aria-label="Zoomed baseline saved time" min={m.first_frame} max={m.times.length-1} value={index} aria-valuetext={playbackStamp(at!,options.zone)} onChange={e=>seek(Number(e.target.value))}/>
            <button aria-label="Next baseline frame" disabled={index>=m.times.length-1} onClick={()=>seek(index+1)}>›</button></div>
          {!!error&&<div role="alert">{error}<button onClick={reopen}>Reload saved sequence</button></div>}
        </div>}
        actions={<button disabled={!ready} onClick={()=>{setPlaying(false);onInspect(m.pairs[expanded.pair].slice().sort((a,b)=>a-b),Math.max(m.times[0],at!-m.window_s),at!,options);}}>Open stored-pair detail</button>}>
        <div className="vis-movie-expanded"><SpectrumClocks at={at} lst={m.lst[index]}/><p>{expanded.a.station} · {wiringLabel(expanded.a)}<br/>{expanded.b.station} · {wiringLabel(expanded.b)} · {options.reference==='raw'?'Raw':'Sun fringe-stopped'}</p>{ready?plot(expanded):<div className="vis-zoom-buffering">{status}</div>}</div>
      </FigureZoom>}
    </section>
  </dialog>,document.body);
}
