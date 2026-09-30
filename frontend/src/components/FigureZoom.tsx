import { ReactNode, useEffect, useRef, useState } from "react";
import { createPortal, flushSync } from "react-dom";
import { downloadFigure } from "./figureDownload";
import "../figure-zoom.css";

/** Display zoom only: never interpolate, re-average, or change scientific axes. */
export function FigureZoom({title,onClose,children,actions,controls,onBeforeDownload,downloadDisabled=false}:{title:string;onClose:()=>void;children:ReactNode;actions?:ReactNode;controls?:ReactNode;onBeforeDownload?:()=>void;downloadDisabled?:boolean}) {
  const dialog=useRef<HTMLDialogElement>(null), viewport=useRef<HTMLDivElement>(null), content=useRef<HTMLDivElement>(null);
  const [scale,setScale]=useState(1);
  const [downloading,setDownloading]=useState(false),[downloadError,setDownloadError]=useState('');
  const drag=useRef<{x:number;y:number;left:number;top:number}|null>(null);
  useEffect(()=>{
    const previous=document.activeElement as HTMLElement|null;
    const el=dialog.current!;el.showModal();
    return()=>{el.close();previous?.focus();};
  },[]);
  const zoom=(n:number)=>setScale(Math.min(4,Math.max(1,n)));
  const download=async()=>{
    if(!content.current||downloadDisabled)return;
    // A moving figure must stop before its title, clocks and pixels are captured.
    if(onBeforeDownload)flushSync(onBeforeDownload);
    setDownloading(true);setDownloadError('');
    try{await downloadFigure(content.current,title);}
    catch(error){setDownloadError(`Download failed: ${(error as Error).message}`);}
    finally{setDownloading(false);}
  };
  return createPortal(<dialog ref={dialog} className="figure-modal" aria-label={title}
    onCancel={e=>{e.preventDefault();onClose();}} onClick={e=>{if(e.target===e.currentTarget)onClose();}}>
    <section>
      <header className="figure-zoom-toolbar"><h3>{title}</h3><button autoFocus onClick={onClose}>Close ×</button></header>
      <div className="figure-zoom-controls">
        <button aria-label="Zoom out" disabled={scale<=1} onClick={()=>zoom(scale-.5)}>−</button>
        <output aria-label="Display zoom">{Math.round(scale*100)}%</output>
        <button aria-label="Zoom in" disabled={scale>=4} onClick={()=>zoom(scale+.5)}>+</button>
        <button onClick={()=>{zoom(1);viewport.current?.scrollTo(0,0);}}>Fit</button>
        <button onClick={download} disabled={downloading||downloadDisabled}>{downloading?'Preparing…':'Download'}</button>
        <span>Display zoom · double-click to zoom · drag or scroll to pan</span>
      </div>
      {controls}
      {downloadError&&<p className="figure-download-error" role="alert">{downloadError}</p>}
      <div ref={viewport} className={`figure-zoom-viewport ${scale>1?'zoomed':''}`}
        onDoubleClick={()=>zoom(scale===1?2:1)}
        onPointerDown={e=>{if(scale<=1||e.button!==0||(e.target as HTMLElement).closest('button,a'))return;const el=e.currentTarget;drag.current={x:e.clientX,y:e.clientY,left:el.scrollLeft,top:el.scrollTop};el.setPointerCapture(e.pointerId);}}
        onPointerMove={e=>{const d=drag.current;if(d){e.currentTarget.scrollLeft=d.left+d.x-e.clientX;e.currentTarget.scrollTop=d.top+d.y-e.clientY;}}}
        onPointerUp={()=>{drag.current=null;}} onPointerCancel={()=>{drag.current=null;}}>
        <div ref={content} className="figure-zoom-content" style={{width:`${scale*100}%`,['--figure-zoom' as string]:scale}}>{children}</div>
      </div>
      {actions&&<div className="figure-zoom-actions">{actions}</div>}
    </section>
  </dialog>,document.body);
}
