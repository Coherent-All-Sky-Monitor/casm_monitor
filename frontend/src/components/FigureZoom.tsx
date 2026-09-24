import { ReactNode, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import "../figure-zoom.css";

/** Display zoom only: never interpolate, re-average, or change scientific axes. */
export function FigureZoom({title,onClose,children,actions}:{title:string;onClose:()=>void;children:ReactNode;actions?:ReactNode}) {
  const dialog=useRef<HTMLDialogElement>(null), viewport=useRef<HTMLDivElement>(null);
  const [scale,setScale]=useState(1);
  const drag=useRef<{x:number;y:number;left:number;top:number}|null>(null);
  useEffect(()=>{
    const previous=document.activeElement as HTMLElement|null;
    const el=dialog.current!;el.showModal();
    return()=>{el.close();previous?.focus();};
  },[]);
  const zoom=(n:number)=>setScale(Math.min(4,Math.max(1,n)));
  return createPortal(<dialog ref={dialog} className="figure-modal" aria-label={title}
    onCancel={e=>{e.preventDefault();onClose();}} onClick={e=>{if(e.target===e.currentTarget)onClose();}}>
    <section>
      <header className="figure-zoom-toolbar"><h3>{title}</h3><button autoFocus onClick={onClose}>Close ×</button></header>
      <div className="figure-zoom-controls">
        <button aria-label="Zoom out" disabled={scale<=1} onClick={()=>zoom(scale-.5)}>−</button>
        <output aria-label="Display zoom">{Math.round(scale*100)}%</output>
        <button aria-label="Zoom in" disabled={scale>=4} onClick={()=>zoom(scale+.5)}>+</button>
        <button onClick={()=>{zoom(1);viewport.current?.scrollTo(0,0);}}>Fit</button>
        <span>Display zoom · double-click to zoom · drag or scroll to pan</span>
      </div>
      <div ref={viewport} className={`figure-zoom-viewport ${scale>1?'zoomed':''}`}
        onDoubleClick={()=>zoom(scale===1?2:1)}
        onPointerDown={e=>{if(scale<=1||e.button!==0||(e.target as HTMLElement).closest('button,a'))return;const el=e.currentTarget;drag.current={x:e.clientX,y:e.clientY,left:el.scrollLeft,top:el.scrollTop};el.setPointerCapture(e.pointerId);}}
        onPointerMove={e=>{const d=drag.current;if(d){e.currentTarget.scrollLeft=d.left+d.x-e.clientX;e.currentTarget.scrollTop=d.top+d.y-e.clientY;}}}
        onPointerUp={()=>{drag.current=null;}} onPointerCancel={()=>{drag.current=null;}}>
        <div className="figure-zoom-content" style={{width:`${scale*100}%`,['--figure-zoom' as string]:scale}}>{children}</div>
      </div>
      {actions&&<div className="figure-zoom-actions">{actions}</div>}
    </section>
  </dialog>,document.body);
}
