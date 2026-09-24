/** Round, in-range axis ticks. This only labels coordinates; it never bins data. */
export function niceTicks(lo:number,hi:number,target=8):number[] {
  if(!Number.isFinite(lo+hi)||hi<=lo)return [];
  const raw=(hi-lo)/Math.max(1,target-1),power=10**Math.floor(Math.log10(raw));
  const step=([1,2,2.5,5,10].find(n=>n*power>=raw*(1-1e-10))??10)*power;
  const first=Math.ceil(lo/step-1e-9),last=Math.floor(hi/step+1e-9);
  const ticks=Array.from({length:Math.max(0,Math.min(100,last-first+1))},(_,i)=>Number(((first+i)*step).toPrecision(12)));
  return ticks.length>=2?ticks:[lo,hi];
}

export function frequencyLabel(value:number):string {return Number(value.toPrecision(9)).toString();}

/** Clock-aligned ticks, including fractional-hour zones via a UTC offset. */
export function timeTicks(lo:number,hi:number,width:number,zone:string):number[] {
  const wanted=Math.max(2,Math.min(16,Math.floor(width/58)));
  const raw=(hi-lo)/wanted;
  const step=[60,120,300,600,900,1800,3600,7200,10800,14400,21600,43200,86400].find(s=>s>=raw)??86400;
  const parts=new Intl.DateTimeFormat('en-US',{timeZone:zone,timeZoneName:'longOffset'}).formatToParts(new Date(lo*1000));
  const offset=/GMT([+-])(\d{2}):(\d{2})/.exec(parts.find(p=>p.type==='timeZoneName')?.value??'');
  const seconds=offset?(offset[1]==='-'?-1:1)*(Number(offset[2])*3600+Number(offset[3])*60):0;
  const first=Math.ceil((lo+seconds)/step)*step-seconds;
  const ticks=Array.from({length:Math.max(0,Math.floor((hi-first)/step)+1)},(_,k)=>first+k*step);
  return ticks.length>=2?ticks:[lo,hi];
}
