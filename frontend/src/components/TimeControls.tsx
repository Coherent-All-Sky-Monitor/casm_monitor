import { useState } from "react";

export const LOCAL = "America/Los_Angeles";
export function wallTime(utc: string, zone: string): string {
  const d = new Date(utc.endsWith("Z") ? utc : utc + "Z");
  if (!Number.isFinite(d.getTime())) return "";
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(d);
  const p = (key: string) => parts.find(x => x.type === key)?.value;
  return `${p("year")}-${p("month")}-${p("day")}T${p("hour")}:${p("minute")}`;
}
export function utcFromWall(wall: string, zone: string): string {
  const naive = Date.parse(wall + "Z");
  const offsets = [-12, 0, 12].map(h => {
    const d = new Date(naive + h * 3600000);
    return Date.parse(wallTime(d.toISOString(), zone) + "Z") - d.getTime();
  });
  const matches = offsets.map(o => new Date(naive - o).toISOString().slice(0,16))
    .filter(s => wallTime(s, zone) === wall).sort();
  if (!matches.length) throw new Error("That local time does not exist across the daylight-saving transition. Select another time or UTC.");
  return matches[0]; // Repeated fall-back hour: earlier occurrence; UTC selects either explicitly.
}
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
