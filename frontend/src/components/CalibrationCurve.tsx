import {useMemo} from 'react';
import {niceTicks, frequencyLabel} from './vis/PlotTicks';
import {number} from './vis/ArrayPlots';
import {signedLog,inverseSignedLog} from './calibrationCurveScale';

export type CalLine = {label:string;values:(number|null)[];color?:string};
const COLORS=['#2459a6','#cc7528','#6b7785','#207c5b'];

/** Native-channel curves; wrap boundaries and missing channels are never joined. */
export function CalibrationCurve({freq,lines,label,phase=false,range,scale='linear'}:{freq:number[];lines:CalLine[];label:string;phase?:boolean;range?:[number,number];scale?:'linear'|'symlog'}) {
  const chart=useMemo(()=>{
    const transform=scale==='symlog'&&!phase?signedLog:(v:number)=>v;
    const inverse=scale==='symlog'&&!phase?inverseSignedLog:(v:number)=>v;
    let lo=Infinity,hi=-Infinity;
    for(const line of lines)for(const v of line.values)if(v!==null&&Number.isFinite(v)){lo=Math.min(lo,transform(v));hi=Math.max(hi,transform(v));}
    if(range){[lo,hi]=range.map(transform);}else if(phase){lo=-Math.PI;hi=Math.PI;}
    else if(!Number.isFinite(lo+hi)){lo=0;hi=1;}
    else {const pad=Math.max((hi-lo)*.06,Math.abs(hi)*.015,1e-9);lo-=pad;hi+=pad;}
    const f0=Math.min(...freq),f1=Math.max(...freq);
    const paths=lines.map(line=>{let path='',previous:number|null=null;line.values.forEach((v,i)=>{
      if(v===null||!Number.isFinite(v)||!Number.isFinite(freq[i])){previous=null;return;}
      const x=70+(freq[i]-f0)/(f1-f0||1)*358,y=16+(hi-transform(v))/(hi-lo)*180;
      path+=(previous===null||(phase&&Math.abs(v-previous)>Math.PI)?'M':'L')+`${x.toFixed(2)},${y.toFixed(2)} `;previous=v;
    });return path;});
    return {lo,hi,f0,f1,paths,inverse,xticks:niceTicks(f0,f1,9),yticks:phase?[-Math.PI,-Math.PI/2,0,Math.PI/2,Math.PI]:niceTicks(lo,hi,6)};
  },[freq,lines,phase,range,scale]);
  const tick=(v:number)=>phase?(v===0?'0':v===Math.PI?'π':v===-Math.PI?'−π':v<0?'−π/2':'π/2'):number(v);
  return <div className="cal-curve" data-scale={scale}><svg viewBox="0 0 450 252" role="img" aria-label={`${label} versus frequency${scale==='symlog'?' · signed-log scale':''}`}>
    <rect width="450" height="252" fill="white"/>
    {chart.yticks.map(v=>{const y=16+(chart.hi-v)/(chart.hi-chart.lo)*180;return <g key={v}><line x1="70" x2="428" y1={y} y2={y} stroke={v===0?'#a9b4c1':'#e5e9ee'}/><text x="63" y={y+4} textAnchor="end">{tick(chart.inverse(v))}</text></g>;})}
    {chart.xticks.map(f=>{const x=70+(f-chart.f0)/(chart.f1-chart.f0)*358;return <g key={f}><line x1={x} x2={x} y1="196" y2="201" stroke="#455363"/><text x={x} y="217" textAnchor="middle">{frequencyLabel(f)}</text></g>;})}
    <path d="M70,16 V196 H428" fill="none" stroke="#455363"/>
    {chart.paths.map((path,i)=><path key={i} d={path} fill="none" stroke={lines[i].color??COLORS[i%COLORS.length]} strokeWidth="1.15"/>)}
    <text x="250" y="242" textAnchor="middle">Frequency (MHz)</text><text transform="translate(17 108) rotate(-90)" textAnchor="middle">{label}</text>
  </svg>{lines.length>1&&<div className="cal-curve-key">{lines.map((line,i)=><span key={line.label}><i style={{background:line.color??COLORS[i%COLORS.length]}}/>{line.label}</span>)}</div>}</div>;
}
