import {useEffect, useState} from 'react';
import {Evidence, Json, Notice, stamp} from './Workspace';

const ROOT='/api/calibration';
type Perform=(url:string, body:Json, success:string, headers?:Record<string,string>)=>Promise<any>;

function parsed(value:string) {
  const match=value?.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2}) UTC/);
  return match?`${match[1]}T${match[2]}Z`:value;
}
function display(value:string, zone:string, part:'date'|'time') {
  const date=new Date(parsed(value));
  if(!Number.isFinite(date.getTime()))return 'Unavailable';
  return new Intl.DateTimeFormat('en-US',{timeZone:zone,...(part==='date'
    ?{month:'short',day:'numeric',year:'numeric'} as const
    :{hour:'2-digit',minute:'2-digit',hour12:false,timeZoneName:'short'} as const)}).format(date);
}

export function CurrentCalibration({value}:{value:Json|null}) {
  const [zone,setZone]=useState('America/Los_Angeles');
  return <section className="cal-current" aria-label="Current deployed calibration">
    <div className="cal-section-heading"><h3>Current deployed calibration</h3><div className="cal-current-zones">
      <button aria-pressed={zone!=='UTC'} onClick={()=>setZone('America/Los_Angeles')}>OVRO local</button>
      <button aria-pressed={zone==='UTC'} onClick={()=>setZone('UTC')}>UTC</button>
    </div></div>
    {value&&!value.unavailable?<>
      <div className="cal-current-grid">
        <div><span>Calibrator · {value.source==='sun'?'Sun':value.source}</span><strong>{display(value.source_window[0],zone,'date')}</strong><small>Calibration observation</small></div>
        <div><span>Solve window</span><strong className="cal-current-time">{display(value.source_window[0],zone,'time').replace(/ (PDT|PST|UTC)$/,'')} – {display(value.source_window[1],zone,'time')}</strong><small>{value.antennas?.length} calibration antennas</small></div>
        <div><span>Last uploaded</span><strong>{display(value.date_uploaded,zone,'date')}</strong><small>{display(value.date_uploaded,zone,'time')}</small></div>
      </div>
    </>:<Notice>{value?.reason??'Loading recorded deployment…'}</Notice>}
  </section>;
}

export default function CalibrationDeployment({tag,detail,deployment,deployDetail,roots,disabled,perform}: {
  tag:string;detail:Json;deployment:Json;deployDetail:Json;roots:string[];disabled:boolean;perform:Perform;
}) {
  const [parent,setParent]=useState(roots[0]??'');
  const [note,setNote]=useState('');
  const [accept,setAccept]=useState(false);
  const [confirm,setConfirm]=useState('');
  const [defaults,setDefaults]=useState(true);
  const [kind,setKind]=useState('both');
  const [scale,setScale]=useState('');
  const [ibScale,setIbScale]=useState('');
  const [streams,setStreams]=useState<number[]>([0,1,2,3,4,5]);
  const [utc,setUtc]=useState('');
  const [copyStatus,setCopyStatus]=useState('Copy folder path');
  const item=detail.export, stage=deployDetail.stage;
  useEffect(()=>{setAccept(false);setConfirm('');setDefaults(true);},[tag,item?.export_id]);
  useEffect(()=>{
    const saved=stage?.export_id===item?.export_id?stage?.options:null;
    setKind(saved?.kind??'both');setScale(String(saved?.scale??deployment.deployed?.scale??''));
    setIbScale(String(saved?.ib_scale??deployment.deployed?.ib_scale??''));
    setStreams(saved?.streams??[0,1,2,3,4,5]);setUtc(saved?.utc_start??'');
    setAccept(false);setConfirm('');
  },[tag,item?.export_id,stage?.digest,deployment.deployed?.scale,deployment.deployed?.ib_scale]);
  useEffect(()=>{if(!parent&&roots[0])setParent(roots[0]);},[roots[0]]);
  const options={kind,scale:Number(scale),ib_scale:Number(ibScale),streams,utc_start:utc.trim()||null};
  const valid=streams.length>0&&[scale,ibScale].every(v=>/^\d+$/.test(v)&&Number(v)>0&&Number(v)<=2147483647)&&(!utc||/^\d{4}-\d{2}-\d{2}-\d{2}:\d{2}:\d{2}$/.test(utc));
  const sameOptions=stage?.options&&['kind','scale','ib_scale','utc_start'].every(key=>stage.options[key]===options[key as keyof typeof options])&&JSON.stringify(stage.options.streams)===JSON.stringify(streams);
  const target=kind==='both'?'CB & IB':kind.toUpperCase();
  const ready=Boolean(detail.summary?.has_weights&&!detail.request?.imported);
  const staged=Boolean(sameOptions&&stage?.checks_ok&&stage.export_id===item?.export_id&&stage.export_manifest_sha256===item?.manifest_sha256);
  const reviewed=Boolean(staged&&detail.review?.accepted&&detail.review.export_id===item?.export_id&&detail.review.stage_digest===stage?.digest);
  const canUpload=reviewed&&deployment.allow_upload&&!deployment.casm_track_running&&!disabled&&confirm===tag;
  const upload=()=>{
    if(!canUpload||!window.confirm(`Upload ${target} for ${tag}?\nStreams: ${streams.join(', ')}\nCB SCALE ${scale} · IB SCALE ${ibScale}\nExport saved: ${stamp(item.created_utc)}\n${defaults?'Also replace restart defaults for this selection.':'Restart defaults will remain unchanged.'}\nThis changes the running beamformer.`))return;
    const cookie=document.cookie.split('; ').find(row=>row.startsWith('casm_monitor_csrf='))?.split('=').slice(1).join('=')??'';
    return perform(`${ROOT}/deployment/builds/${tag}/upload`,{confirm_tag:confirm,save_defaults:defaults,note,stage_digest:stage.digest},
      'Upload requested for the reviewed product. Check the job and transfer receipts.',{'X-CSRF-Token':decodeURIComponent(cookie)});
  };
  return <div className="cal-release" aria-label="Export and deployment">
    <section className="cal-card">
      <div className="cal-section-heading"><h3>Write CB & IB weights to disk</h3><span>{item?'Export saved':'1 · Export'}</span></div>
      <p className="cal-caption">Save CB & IB, a complete calibration record notebook, solve and grid reports, static template and diagnostic arrays in a new dated directory. The notebook includes ordered replay cells through writing both weights to disk.</p>
      <label>Export parent directory<input list="cal-export-roots" value={parent} onChange={e=>setParent(e.target.value)} spellCheck={false}/></label>
      <datalist id="cal-export-roots">{roots.map(root=><option key={root} value={root}/>)}</datalist>
      <p className="cal-caption">Each export gets its own dated folder. Choose a listed root or a subdirectory within it.</p>
      <button className="cal-primary" disabled={disabled||!ready||!parent} onClick={()=>perform(`${ROOT}/builds/${tag}/export`,{parent},'Weight export queued. No upload requested.')}>Write CB & IB to disk</button>
      {!ready&&<p className="cal-caption">Generate the 512 beams and matching IB in step 4 first.</p>}
      {item&&<div className="cal-export-receipt"><strong>Saved weights · {tag}</strong><p className="cal-caption">Written {stamp(item.created_utc)}</p>
        <button onClick={()=>{if(!navigator.clipboard){setCopyStatus('Full path is in metadata below');return;}navigator.clipboard.writeText(item.directory).then(()=>setCopyStatus('Folder path copied')).catch(()=>setCopyStatus('Full path is in metadata below'));}}>{copyStatus}</button>
        {(detail.artifacts??[]).filter((a:Json)=>a.name==='calibration_record.ipynb'&&a.relative.includes(item.export_id)).map((a:Json)=><p key={a.id}><a className="cal-primary" href={a.url} download>Download full calibration notebook</a></p>)}
        {!item.files.some((f:Json)=>f.role==='calibration_record')&&<p className="cal-caption">This older export has the grid notebook only. Write a new export to include the full calibration record; the existing export will be preserved.</p>}
        <div className="cal-export-files">{item.files.filter((f:Json)=>['CB','IB','cal_h5'].includes(f.role)).map((file:Json)=><div key={file.role}><b>{file.role==='cal_h5'?'Calibration':file.role}</b><code>{file.relative}</code></div>)}</div>
        <p className="cal-caption">Full paths and hashes are in manifest.json. Inspect these files before approving deployment.</p><Evidence value={item}/>
      </div>}
    </section>
    <section className="cal-card cal-deploy-options">
      <div className="cal-section-heading"><h3>Choose what to upload</h3><span>{detail.summary?.antennas?.length} antennas · {streams.length} streams</span></div>
      <div className="segmented" role="group" aria-label="Weights to upload">{[['both','CB & IB'],['cb','CB only'],['ib','IB only']].map(([value,label])=><button type="button" key={value} disabled={disabled} className={kind===value?'selected':''} aria-pressed={kind===value} onClick={()=>setKind(value)}>{label}</button>)}</div>
      <div className="cal-columns">
        <label>CB SCALE<input type="number" min="1" max="2147483647" step="1" value={scale} disabled={disabled||kind==='ib'} onChange={e=>setScale(e.target.value)}/></label>
        <label>IB SCALE<input type="number" min="1" max="2147483647" step="1" value={ibScale} disabled={disabled||kind==='cb'} onChange={e=>setIbScale(e.target.value)}/></label>
      </div>
      <p className="cal-caption">Current recorded scales: CB {deployment.deployed?.scale??'—'} / IB {deployment.deployed?.ib_scale??'—'}. Changing a scale changes output power. A new dry run binds your selection.</p>
      {kind!=='both'&&<p className="cal-transit-warning">{kind==='cb'?'IB':'CB'} weights and defaults stay unchanged. Check that the selected scale and antenna set remain suitable for the existing companion.</p>}
      <details><summary>Advanced · streams, start time & command</summary>
        <fieldset disabled={disabled}><legend>Streams</legend><div className="cal-deploy-streams">{[0,1,2,3,4,5].map(s=><label className="cal-check" key={s}><input type="checkbox" checked={streams.includes(s)} onChange={e=>setStreams(current=>e.target.checked?[...current,s].sort():current.filter(v=>v!==s))}/>{s} · corr{s<3?'1':'2'}</label>)}</div></fieldset>
        <label>UTC_START · blank for upload time<input value={utc} disabled={disabled} placeholder="YYYY-MM-DD-HH:MM:SS" onChange={e=>setUtc(e.target.value)} spellCheck={false}/></label>
        <p className="cal-caption">This is the uploader’s UTC_START header, not a scheduled webpage job. A supplied time must still be in the future when uploading.</p>
        {utc.trim()&&<p className="cal-transit-warning">The registry records transfer time, not future activation. Antenna and beam displays may switch before this UTC_START; avoid interpreting that interval as a verified active configuration.</p>}
        <p className="cal-caption">HDF5 inputs come from the verified export. Each dry run gets a fresh payload directory. IB destination: /tmp/incohweights.fifo.{'{stream}'}. Registry recording is required for live CB uploads; dry runs use --no-registry.</p>
        {stage&&<><p className="cal-path">{stage.stage_dir}</p><pre className="cal-upload-command">{stage.command}{defaults?' --save-defaults':''}</pre></>}
      </details>
    </section>
    <div className="cal-columns">
      <section className="cal-card"><div className="cal-section-heading"><h3>Upload dry run</h3><span>{staged?'Checks passed':'2 · Test locally'}</span></div>
        <p className="cal-caption">Build and check {target} payloads for {streams.length} streams from this export. Nothing is transferred or saved as telescope defaults.</p>
        <button disabled={disabled||!item||!ready||!valid} onClick={()=>perform(`${ROOT}/deployment/builds/${tag}/stage`,{export_id:item.export_id,save_defaults:false,options},'Dry run queued for this selection. Nothing is sent to the telescope.')}>Run {target} dry run</button>
        {stage&&!sameOptions&&<p className="cal-transit-warning">Selection changed or the previous dry run predates these controls. Run a new dry run and review it.</p>}
        {stage&&<><p className="cal-caption">{staged?'Passed for this export.':'This dry run is failed or belongs to an earlier export.'}</p>
          <div className="cal-checklist">{stage.checks?.map((check:Json)=><p key={check.name}><span className={check.ok?'complete':''}>{check.ok?'✓':'!'}</span>{check.name.replaceAll('_',' ')}</p>)}</div><Evidence value={stage}/></>}
      </section>
      <section className="cal-card"><div className="cal-section-heading"><h3>Your review</h3><span>{reviewed?'Approved':'3 · Inspect & approve'}</span></div>
        <p className="cal-caption">Compare the exported CB/IB pair and sky diagnostics with the old weights, then record your decision here.</p>
        <label>Review note<textarea value={note} onChange={e=>{setNote(e.target.value);setAccept(false);}} placeholder="What did you compare, and is this product ready?"/></label>
        <label className="cal-check"><input type="checkbox" checked={accept} onChange={e=>setAccept(e.target.checked)}/>I vetted the exported files and selected {target} payloads, streams and scales.</label>
        <button disabled={disabled||!staged||!accept||!note.trim()} onClick={()=>perform(`${ROOT}/builds/${tag}/review`,{accepted:true,note,export_id:item.export_id,stage_digest:stage.digest},'Review saved for this exact export and dry run. Nothing uploaded.')}>Save review</button>
        {reviewed&&<p className="cal-caption">Saved {stamp(detail.review.at)} · {detail.review.note}</p>}
      </section>
    </div>
    <section className="cal-card cal-release-upload"><div className="cal-section-heading"><h3>Upload to the system</h3><span>{deployment.allow_upload?'4 · Explicit confirmation':'Locked · pending deployment approval'}</span></div>
      {!deployment.allow_upload&&<p className="cal-caption">Live upload remains disabled on the server while you vet the files. Once deployment is enabled, use the confirmation below.</p>}
      {deployment.casm_track_running&&<Notice>A tracking-beam process is running or its status is unavailable. Upload is blocked.</Notice>}
      <label>Type the build ID to confirm<input value={confirm} onChange={e=>setConfirm(e.target.value)} placeholder={tag} autoComplete="off" spellCheck={false}/></label>
      <label className="cal-check"><input type="checkbox" checked={defaults} onChange={e=>setDefaults(e.target.checked)}/>Save as defaults · selected weights and streams</label>
      <button className="cal-primary" disabled={!canUpload} onClick={upload}>Upload reviewed {target}</button>
      <p className="cal-caption">The server rechecks file hashes, paired weights, scales, layout, review and upload authorization. Transfer, registry and runtime activation are separate checks.</p>
      {deployDetail.uploads?.length>0&&<Evidence value={deployDetail.uploads}/>}
    </section>
  </div>;
}
