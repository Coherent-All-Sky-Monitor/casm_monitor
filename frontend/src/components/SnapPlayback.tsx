import {useEffect,useMemo,useRef,useState} from 'react';
import {createPortal} from 'react-dom';
import {Json,Notice,useResource} from './Workspace';
import {FigureZoom} from './FigureZoom';
import {SnapPlot} from './SnapPlot';
import {SpectrumClocks} from './SpectrumClocks';
import {snapRange,SNAP_REFERENCE_POWER} from './snapPower';
import {SnapSnapshot,snapDay,useSnapResource} from './SnapHistory';
import {decodePlaybackBatch,PLAYBACK_BATCH_SIZE,playbackColumns,playbackGap,playbackNext,playbackStamp,PlaybackTimeZone} from './snapPlaybackModel';
import '../snap-playback.css';

export type PlaybackInput={key:string;ip:string;adc:number;snap:number;slot:string;station:string|null;antenna:number|null;beamforming:boolean|null};
type Frame={index:number;data:Json};
type Preferences={zone:PlaybackTimeZone;speed:number;loop:boolean;span:number};

/** Current + next batch only. Stepping through buffered frames makes no request. */
function useFrames(snapshots:SnapSnapshot[],index:number,manifest:Json|null,inputs:PlaybackInput[],loop:boolean){
  const [,render]=useState(0),[error,setError]=useState(''),[retry,setRetry]=useState(0);
  const cache=useRef(new Map<number,Json[]>()), pending=useRef(new Map<number,{promise:Promise<Json[]>;controller:AbortController}>());
  const generation=useRef(0),lastShown=useRef<Frame|null>(null);
  const base=Math.floor(index/PLAYBACK_BATCH_SIZE)*PLAYBACK_BATCH_SIZE;
  const nextBase=base+PLAYBACK_BATCH_SIZE<snapshots.length?base+PLAYBACK_BATCH_SIZE:loop&&base>0?0:null;
  const wantedBases=useRef([base,nextBase]);wantedBases.current=[base,nextBase];
  useEffect(()=>{
    generation.current++;cache.current.clear();lastShown.current=null;render(v=>v+1);
    return()=>{generation.current++;for(const p of pending.current.values())p.controller.abort();pending.current.clear();};
  },[manifest]);
  useEffect(()=>{
    if(!snapshots.length)return;
    let active=true;
    const token=generation.current;
    const wanted=(i:number)=>wantedBases.current.includes(i);
    for(const [i,p] of pending.current)if(!wanted(i)){p.controller.abort();pending.current.delete(i);}
    for(const i of cache.current.keys())if(!wanted(i))cache.current.delete(i);
    const fetchBatch=(i:number):Promise<Json[]>=>{
      if(cache.current.has(i))return Promise.resolve(cache.current.get(i)!);
      const existing=pending.current.get(i);if(existing)return existing.promise;
      const controller=new AbortController();
      const selection=snapshots.slice(i,i+PLAYBACK_BATCH_SIZE),keys=inputs.map(input=>input.key);
      const query=new URLSearchParams({frames:selection.map(s=>s.ids.join(',')).join(';'),inputs:keys.join(',')});
      const promise=fetch(`/api/snap-workspace/spectra-playback-frames?${query}`,{signal:controller.signal}).then(async r=>{
        if(!r.ok)throw new Error(`Saved reads unavailable (HTTP ${r.status})`);
        const data=decodePlaybackBatch(await r.arrayBuffer(),keys);
        if(JSON.stringify(data.map(f=>f.ids))!==JSON.stringify(selection.map(s=>s.ids)))throw new Error('Playback returned different saved reads');
        return data;
      }).then(data=>{
        if(!controller.signal.aborted&&token===generation.current&&wanted(i)){
          cache.current.set(i,data);render(v=>v+1);
        }
        return data;
      });
      pending.current.set(i,{promise,controller});
      promise.finally(()=>{if(pending.current.get(i)?.promise===promise)pending.current.delete(i);}).catch(()=>{});
      return promise;
    };
    setError('');
    void fetchBatch(base).then(async()=>{
      if(!active)return;
      if(nextBase!==null){
        try{await fetchBatch(nextBase);}catch{/* retry/report only if this batch becomes foreground */}
      }
    }).catch(e=>{if(active)setError((e as Error).message);});
    return()=>{active=false;};
  },[manifest,base,nextBase,retry,inputs]);
  const data=cache.current.get(base)?.[index-base];
  const frame=data?{index,data}:lastShown.current;
  if(data)lastShown.current=frame;
  return {frame,error,retry:()=>setRetry(v=>v+1)};
}

export function SnapPlayback({day:initialDay,inputs,onClose,onSelect}:{day:string;inputs:PlaybackInput[];onClose:()=>void;onSelect:(s:SnapSnapshot)=>void}){
  const dialog=useRef<HTMLDialogElement>(null);
  const [day,setDay]=useState(initialDay);
  const [preferences,setPreferences]=useState<Preferences>({zone:'UTC',speed:1,loop:false,span:1});
  useEffect(()=>{
    const previous=document.activeElement as HTMLElement|null,el=dialog.current!;
    el.showModal();const overflow=document.body.style.overflow;document.body.style.overflow='hidden';
    return()=>{el.close();document.body.style.overflow=overflow;previous?.focus();};
  },[]);
  return createPortal(<dialog ref={dialog} className="snap-movie" aria-label="SNAP history playback" onCancel={e=>{if(e.target===e.currentTarget){e.preventDefault();onClose();}}}>
    <PlaybackDay key={`${day}/${preferences.span}`} day={day} inputs={inputs} onDay={setDay} onClose={onClose} onSelect={onSelect} preferences={preferences} onPreferences={setPreferences}/>
  </dialog>,document.body);
}

function PlaybackDay({day,inputs,onDay,onClose,onSelect,preferences,onPreferences}:{day:string;inputs:PlaybackInput[];onDay:(d:string)=>void;onClose:()=>void;onSelect:(s:SnapSnapshot)=>void;preferences:Preferences;onPreferences:(p:Preferences)=>void}){
  const dates=useSnapResource('/api/snap-workspace/spectra-days',60000);
  // Fresh when opened, then pinned for the whole session, even across an
  // hourly acquisition. Never replace the sequence or its axes mid-play.
  const manifest=useResource(`/api/snap-workspace/spectra-playback?day=${day}&span_days=${preferences.span}`);
  const snapshots:SnapSnapshot[]=useMemo(()=>(manifest.data?.snapshots??[]).map((s:SnapSnapshot)=>({...s,day:snapDay(s.at)})),[manifest.data]);
  const [index,setIndex]=useState(0),[playing,setPlaying]=useState(true),[expanded,setExpanded]=useState<PlaybackInput|null>(null);
  const {speed,loop,zone,span}=preferences;
  const [size,setSize]=useState({width:1200,height:700});
  const stage=useRef<HTMLDivElement>(null), seen=useRef(new Map<string,string>());
  const deadline=useRef<number|null>(null),previousSpeed=useRef(speed);
  const {frame,error,retry}=useFrames(snapshots,index,manifest.data,inputs,loop);
  const ready=frame?.index===index&&!error;
  const shown=frame?snapshots[frame.index]:null;
  const range=useMemo(()=>snapRange(inputs.flatMap(i=>manifest.data?.limits_db?.[i.key]??[]),SNAP_REFERENCE_POWER),[manifest.data,inputs]);
  const columns=playbackColumns(inputs.length,size.width,size.height),rows=Math.ceil(inputs.length/columns);
  const days:Json[]=dates.data?.days??[];
  const earlier=days.find(d=>d.day<day)?.day,later=[...days].reverse().find(d=>d.day>day)?.day;
  const pick=(i:number)=>{setPlaying(false);setIndex(i);};
  const toggle=()=>{if(playing)setPlaying(false);else{if(index===snapshots.length-1)setIndex(0);setPlaying(true);}};
  useEffect(()=>{
    const el=stage.current!;
    const measure=()=>{if(el.clientWidth&&el.clientHeight)setSize({width:el.clientWidth,height:el.clientHeight});};
    measure();const timer=window.setTimeout(measure,0); // parent opens the dialog after child effects
    const observer=new ResizeObserver(measure);observer.observe(el);window.addEventListener('resize',measure);
    return()=>{observer.disconnect();window.clearTimeout(timer);window.removeEventListener('resize',measure);};
  },[]);
  useEffect(()=>{if(error)setPlaying(false);},[error]);
  useEffect(()=>{
    const hide=()=>{if(document.hidden)setPlaying(false);};document.addEventListener('visibilitychange',hide);
    return()=>document.removeEventListener('visibilitychange',hide);
  },[]);
  useEffect(()=>{
    if(!playing||!ready||expanded||document.hidden){deadline.current=null;return;}
    if(previousSpeed.current!==speed){deadline.current=null;previousSpeed.current=speed;}
    const period=1000/speed;
    if(deadline.current===null)deadline.current=performance.now()+period;
    const timer=window.setTimeout(()=>{
      // Include rendering time in the interval, rather than adding it to each
      // frame's dwell. Slow buffers still wait, and no saved read is skipped.
      deadline.current=performance.now()+period;
      const next=playbackNext(index,snapshots.length,loop);
      if(next===null)setPlaying(false);else setIndex(next);
    },Math.max(0,deadline.current-performance.now()));
    return()=>window.clearTimeout(timer);
  },[playing,ready,index,snapshots.length,speed,loop,expanded]);
  const changed=(frame?.data.boards??[]).filter((b:Json)=>{
    if(b.ts===null)return false;
    const identity=JSON.stringify([b.eq_epoch,b.fft_shift]);
    if(!seen.current.has(b.ip))seen.current.set(b.ip,identity);
    return seen.current.get(b.ip)!==identity;
  });
  const boardFor=(input:PlaybackInput)=>frame?.data.boards.find((b:Json)=>b.ip===input.ip);
  const missing=inputs.filter(i=>!boardFor(i)?.spectra_db?.[i.adc]).length;
  const gap=frame&&frame.index>0?playbackGap(snapshots[frame.index-1].at,snapshots[frame.index].start,manifest.data?.configured_interval_s??3600):null;
  const status=manifest.error||error?'Playback unavailable':!manifest.data?'Preparing fixed axes…':!snapshots.length?'No saved reads in this interval':!ready?'Buffering saved reads…':playing?'Playing':index===snapshots.length-1?'End of playback':'Paused';
  return <section className="snap-movie-content">
    <header className="snap-movie-header"><div><h2>SNAP history</h2><span>{inputs.length} inputs · current wiring · fixed axes · dB re 10⁻¹⁰ native</span></div>
      <div className="snap-movie-clock">{shown?<SpectrumClocks at={shown.at} lst={shown.lst} dark/>:<strong>{day}</strong>}<small>{status}{shown?` · read ${frame!.index+1} / ${snapshots.length}`:''}</small></div>
      <button onClick={onClose} autoFocus aria-label="Close playback">Close ×</button>
    </header>
    <div className="snap-movie-controls">
      <button className="primary snap-movie-play" onClick={toggle} disabled={(!ready&&!playing)||snapshots.length<2} aria-label={playing?'Pause playback':index===snapshots.length-1?'Replay history':'Play playback'}>{playing?'Pause':index===snapshots.length-1?'Replay':'Play'}</button>
      <div className="snap-movie-range" aria-label="Playback interval">{[[1,'Day'],[2,'Last 48 hours'],[7,'Last week']].map(([value,label])=><button key={value} aria-pressed={span===value} onClick={()=>onPreferences({...preferences,span:Number(value)})}>{label}</button>)}</div>
      <div className="snap-movie-date"><button title="Previous saved day" aria-label="Playback previous day" disabled={!earlier} onClick={()=>onDay(earlier)}>‹</button><label>{span===1?'Day':'Through'} · UTC<input type="date" aria-label="Playback date (UTC)" value={day} min={days[days.length-1]?.day} max={days[0]?.day} onChange={e=>{if(e.target.value)onDay(e.target.value);}}/></label><button title="Next saved day" aria-label="Playback next day" disabled={!later} onClick={()=>onDay(later)}>›</button></div>
      <label>Speed<select aria-label="Playback speed" value={speed} onChange={e=>onPreferences({...preferences,speed:Number(e.target.value)})}>{[.5,1,2,4].map(s=><option key={s} value={s}>{s}×</option>)}</select></label>
      <label>Plot timestamps<select aria-label="Playback time zone" value={zone} onChange={e=>onPreferences({...preferences,zone:e.target.value as PlaybackTimeZone})}><option value="UTC">UTC</option><option value="America/Los_Angeles">OVRO local</option></select></label>
      <label className="snap-movie-loop"><input type="checkbox" checked={loop} onChange={e=>onPreferences({...preferences,loop:e.target.checked})}/>Loop</label>
      <span className="snap-movie-cadence">1× = 1 s per saved read</span>
      <button disabled={!ready} onClick={()=>{if(shown){onSelect(shown);onClose();}}}>Open this read</button>
    </div>
    <div className="snap-movie-timeline" title={manifest.data?`${playbackStamp(manifest.data.start,zone)} to ${playbackStamp(manifest.data.end,zone)}`:undefined}><button aria-label="Playback previous read" disabled={!snapshots.length||index===0} onClick={()=>pick(index-1)}>‹</button><input aria-label="Playback saved time" type="range" min={0} max={Math.max(0,snapshots.length-1)} value={index} disabled={snapshots.length<2} aria-valuetext={snapshots[index]?playbackStamp(snapshots[index].at,zone):'No saved read'} onChange={e=>pick(Number(e.target.value))}/><button aria-label="Playback next read" disabled={!snapshots.length||index===snapshots.length-1} onClick={()=>pick(index+1)}>›</button><span>{gap??'Saved reads only · no interpolation'}{missing&&shown?` · ${missing} inputs without a saved spectrum`:''}{changed.length?` · EQ/FFT differs from first saved setting on SNAP ${changed.map((b:Json)=>b.feng_id).join(', ')}`:''}</span></div>
    {(manifest.error||error)&&<div className="snap-movie-error"><Notice>{manifest.error||error}</Notice>{error&&<button onClick={retry}>Retry saved read</button>}</div>}
    <div className={`snap-movie-stage${!ready?' buffering':''}`} ref={stage} aria-busy={!ready&&!!snapshots.length}>
      {frame&&shown?<div className="snap-movie-grid" style={{gridTemplateColumns:`repeat(${columns},minmax(0,1fr))`,gridTemplateRows:`repeat(${rows},minmax(155px,1fr))`}}>{inputs.map(input=>{
        const b=boardFor(input);
        return <article key={input.key} className={`snap-movie-card${input.beamforming?' in-beamforming':''}`} data-input-key={input.key}>
          <header><strong>{input.station??`ADC ${input.adc}`}</strong><span>SNAP {input.snap} · ADC {input.adc}</span></header>
          <button className="snap-plot-trigger" aria-label={`Expand playback SNAP ${input.snap} ADC ${input.adc}`} onClick={()=>{setPlaying(false);setExpanded(input);}}><SnapPlot compact freq={frame.data.freq_mhz} values={b?.spectra_db?.[input.adc]??null} range={range}/></button>
          <footer>{b?.ts!=null?playbackStamp(b.ts,zone):'No board read in this snapshot'}{b?.error?' · unavailable':''}{b?.zero_channels?.[input.adc]?` · ${b.zero_channels[input.adc]} zero channels`:''}{b?.invalid_channels?.[input.adc]?` · ${b.invalid_channels[input.adc]} invalid channels`:''}</footer>
        </article>;
      })}</div>:<div className="snap-movie-empty">{status}</div>}
    </div>
    {expanded&&frame&&shown&&<FigureZoom title={`${expanded.station??`ADC ${expanded.adc}`} · ${playbackStamp(shown.at,zone)}`} onClose={()=>setExpanded(null)}><div className="snap-expanded"><p>SNAP {expanded.snap} SLOT {expanded.slot} ADC {expanded.adc}</p><SpectrumClocks at={boardFor(expanded)?.ts} lst={boardFor(expanded)?.lst}/><SnapPlot freq={frame.data.freq_mhz} values={boardFor(expanded)?.spectra_db?.[expanded.adc]??null} range={range}/></div></FigureZoom>}
  </section>;
}
