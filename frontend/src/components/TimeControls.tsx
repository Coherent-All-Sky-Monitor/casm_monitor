import { useState } from "react";
import { LOCAL, utcFromWall, wallTime } from "./timeZones";
export { LOCAL, utcFromWall, wallTime } from "./timeZones";
export function TimeZone({value,onChange}:{value:string;onChange:(v:string)=>void}) {
  return <label>Plot and selection time<select aria-label="Time zone" value={value} onChange={e=>onChange(e.target.value)}><option value={LOCAL}>OVRO local · PDT/PST</option><option value="UTC">UTC</option></select></label>;
}
export function TimeWindow({value,onChange,timeZone=LOCAL}:{value:{t0:string;t1:string};onChange:(v:{t0:string;t1:string})=>void;timeZone?:string}) {
  const [error,setError] = useState("");
  const label = timeZone === "UTC" ? "UTC" : "OVRO local";
  const update = (key:"t0"|"t1", wall:string) => {
    try {onChange({...value,[key]:utcFromWall(wall,timeZone)});setError("");} catch(e) {setError((e as Error).message);}
  };
  const day = (date:string) => {
    try {
      const next = new Date(Date.parse(date+"T00:00Z")+86400000).toISOString().slice(0,10);
      onChange({t0:utcFromWall(date+"T00:00",timeZone),t1:new Date(Math.min(Date.now(),Date.parse(utcFromWall(next+"T00:00",timeZone)+"Z"))).toISOString().slice(0,16)});
      setError("");
    } catch(e) {setError((e as Error).message);}
  };
  return <div className="time-controls"><div className="choice-row">
    <button onClick={()=>day(wallTime(new Date().toISOString(),timeZone).slice(0,10))}>Today · {label}</button>
    {[1,24,168].map(h=><button key={h} onClick={()=>onChange({t0:new Date(Date.now()-h*3600000).toISOString().slice(0,16),t1:new Date().toISOString().slice(0,16)})}>{h===1?"Last hour":h===24?"Last 24 h":"Last week"}</button>)}
    <label>{label} day<input aria-label="Observation day" type="date" value={wallTime(value.t0,timeZone).slice(0,10)} onChange={e=>e.target.value&&day(e.target.value)}/></label>
  </div><div className="field-row">{(["t0","t1"] as const).map(key=><label key={key}>{key==="t0"?"From":"To"} · {label}<input aria-label={key==="t0"?"From time":"To time"} type="datetime-local" value={wallTime(value[key],timeZone)} onChange={e=>e.target.value&&update(key,e.target.value)}/></label>)}</div>{error&&<p role="alert">{error}</p>}</div>;
}
