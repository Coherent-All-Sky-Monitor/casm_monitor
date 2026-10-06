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
      </div><p className="cal-caption">Recorded deployed product · {value.product_id??'ID unavailable'}. New trials leave it unchanged.</p>
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
  const [defaults,setDefaults]=useState(false);
  const item=detail.export, stage=deployDetail.stage;
  useEffect(()=>{setAccept(false);setConfirm('');setDefaults(false);},[tag,item?.export_id,stage?.digest]);
  useEffect(()=>{if(!parent&&roots[0])setParent(roots[0]);},[roots[0]]);
  const ready=Boolean(detail.summary?.has_weights&&!detail.request?.imported);
  const staged=Boolean(stage?.checks_ok&&stage.export_id===item?.export_id&&stage.export_manifest_sha256===item?.manifest_sha256);
  const reviewed=Boolean(staged&&detail.review?.accepted&&detail.review.export_id===item?.export_id&&detail.review.stage_digest===stage?.digest);
  const canUpload=reviewed&&deployment.allow_upload&&!deployment.casm_track_running&&!disabled&&confirm===tag;
  const upload=()=>{
    if(!canUpload||!window.confirm(`Upload CB and IB for ${tag}?\nExport: ${item.export_id}\n${defaults?'Also replace restart defaults.':'Restart defaults will remain unchanged.'}\nThis changes the running beamformer.`))return;
    const cookie=document.cookie.split('; ').find(row=>row.startsWith('casm_monitor_csrf='))?.split('=').slice(1).join('=')??'';
    return perform(`${ROOT}/deployment/builds/${tag}/upload`,{confirm_tag:confirm,save_defaults:defaults,note},
      'Upload requested for the reviewed product. Check the job and transfer receipts.',{'X-CSRF-Token':decodeURIComponent(cookie)});
  };
  return <div className="cal-release" aria-label="Export and deployment">
    <section className="cal-card">
      <div className="cal-section-heading"><h3>Write CB & IB weights to disk</h3><span>{item?'Export saved':'1 · Export'}</span></div>
      <p className="cal-caption">Copy the generated weights, calibration, notebook and recipe into a new dated directory. Existing exports are preserved.</p>
      <label>Export parent directory<input list="cal-export-roots" value={parent} onChange={e=>setParent(e.target.value)} spellCheck={false}/></label>
      <datalist id="cal-export-roots">{roots.map(root=><option key={root} value={root}/>)}</datalist>
      <p className="cal-caption">Folder: calibration date / build ID + export time + unique ID. Choose a listed root or a subdirectory within it.</p>
      <button className="cal-primary" disabled={disabled||!ready||!parent} onClick={()=>perform(`${ROOT}/builds/${tag}/export`,{parent},'Weight export queued. No upload requested.')}>Write CB & IB to disk</button>
      {!ready&&<p className="cal-caption">Generate the 512 beams and matching IB in step 4 first.</p>}
      {item&&<div className="cal-export-receipt"><strong>{item.directory}</strong><p className="cal-caption">Written {stamp(item.created_utc)} · ID {item.export_id}</p>
        <div className="cal-export-files">{item.files.filter((f:Json)=>['CB','IB','cal_h5'].includes(f.role)).map((file:Json)=><div key={file.role}><b>{file.role==='cal_h5'?'Calibration':file.role}</b><code>{file.relative}</code><small>SHA-256 {file.sha256.slice(0,16)}…</small></div>)}</div>
        <p className="cal-caption">Full paths and hashes are in manifest.json. Inspect these files before approving deployment.</p><Evidence value={item}/>
      </div>}
    </section>
    <div className="cal-columns">
      <section className="cal-card"><div className="cal-section-heading"><h3>Upload dry run</h3><span>{staged?'Checks passed':'2 · Test locally'}</span></div>
        <p className="cal-caption">Build and check six CB and six IB payloads from this exact exported pair. No transfer or restart-default changes.</p>
        <button disabled={disabled||!item||!ready} onClick={()=>perform(`${ROOT}/deployment/builds/${tag}/stage`,{export_id:item.export_id,save_defaults:false},'Dry run queued for this export. Nothing is sent to the telescope.')}>Run upload dry run</button>
        {stage&&<><p className="cal-caption">{staged?'Passed for this export.':'This dry run is failed or belongs to an earlier export.'}</p><p className="cal-path">{stage.stage_dir}</p>
          <div className="cal-checklist">{stage.checks?.map((check:Json)=><p key={check.name}><span className={check.ok?'complete':''}>{check.ok?'✓':'!'}</span>{check.name.replaceAll('_',' ')}</p>)}</div><Evidence value={stage}/></>}
      </section>
      <section className="cal-card"><div className="cal-section-heading"><h3>Your review</h3><span>{reviewed?'Approved':'3 · Inspect & approve'}</span></div>
        <p className="cal-caption">Compare the exported CB/IB pair and sky diagnostics with the old weights, then record your decision here.</p>
        <label>Review note<textarea value={note} onChange={e=>{setNote(e.target.value);setAccept(false);}} placeholder="What did you compare, and is this product ready?"/></label>
        <label className="cal-check"><input type="checkbox" checked={accept} onChange={e=>setAccept(e.target.checked)}/>I vetted this exported CB/IB pair and approve deployment.</label>
        <button disabled={disabled||!staged||!accept||!note.trim()} onClick={()=>perform(`${ROOT}/builds/${tag}/review`,{accepted:true,note,export_id:item.export_id,stage_digest:stage.digest},'Review saved for this exact export and dry run. Nothing uploaded.')}>Save review</button>
        {reviewed&&<p className="cal-caption">Saved {stamp(detail.review.at)} · {detail.review.note}</p>}
      </section>
    </div>
    <section className="cal-card cal-release-upload"><div className="cal-section-heading"><h3>Upload to the system</h3><span>{deployment.allow_upload?'4 · Explicit confirmation':'Locked · pending deployment approval'}</span></div>
      {!deployment.allow_upload&&<p className="cal-caption">Live upload remains disabled on the server while you vet the files. Once deployment is enabled, use the confirmation below.</p>}
      {deployment.casm_track_running&&<Notice>A tracking-beam process is running or its status is unavailable. Upload is blocked.</Notice>}
      <label>Type the build ID to confirm<input value={confirm} onChange={e=>setConfirm(e.target.value)} placeholder={tag} autoComplete="off" spellCheck={false}/></label>
      <label className="cal-check"><input type="checkbox" checked={defaults} onChange={e=>setDefaults(e.target.checked)}/>Also save these weights as restart defaults on both nodes.</label>
      <button className="cal-primary" disabled={!canUpload} onClick={upload}>Upload reviewed CB & IB</button>
      <p className="cal-caption">The server rechecks file hashes, paired weights, scales, layout, review and upload authorization. Transfer, registry and runtime activation are separate checks.</p>
      {deployDetail.uploads?.length>0&&<Evidence value={deployDetail.uploads}/>}
    </section>
  </div>;
}
