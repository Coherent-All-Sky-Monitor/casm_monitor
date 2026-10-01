import { useEffect, useId, useState } from 'react';
import { api } from './Workspace';
import { FigureZoom } from './FigureZoom';
import { playbackStamp, type PlaybackTimeZone } from './snapPlaybackModel';
import { wallTime } from './timeZones';
import { timeTicks } from './vis/PlotTicks';

type Track = {id: string; label: string; cap_deg: number; altitude_deg: number[]};
type Window = {start: number; end: number; selection: number[]; minutes: number};
type Plan = {date: string; times: number[]; tracks: Track[]; windows: Window[]};
const cache = new Map<string, Promise<Plan>>();
const COLORS = ['#b46b00', '#1468a6', '#287348', '#ad3830'];
const DASHES = ['', '8 3', '3 3', '9 3 2 3'];
function loadPlan(date: string) {
  if (!cache.has(date)) {
    const request = api<Plan>(`/api/calibration/sky-plan?date=${encodeURIComponent(date)}`).catch(error => {cache.delete(date); throw error;});
    cache.set(date, request);
    if (cache.size > 16) cache.delete(cache.keys().next().value!);
  }
  return cache.get(date)!;
}
function unix(value: string) {
  return Date.parse(value && !/(Z|[+-]\d\d:\d\d)$/.test(value) ? value.replace(' ', 'T') + 'Z' : value) / 1000;
}
const iso = (ts: number) => new Date(ts * 1000).toISOString();

function AltitudePlot({data, zone, solve, staticWindow}: {data: Plan; zone: PlaybackTimeZone; solve: string[]; staticWindow: string[] | null}) {
  const id = useId().replace(/:/g, '');
  const [cursor, setCursor] = useState<number | null>(null);
  const [a, b] = [data.times[0], data.times[data.times.length - 1]];
  const x = (t: number) => 62 + (t - a) / (b - a) * 694;
  const y = (v: number) => 58 + (90 - v) / 90 * 236;
  const altitudeTicks = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90];
  const spans = (window: number[], fill: string, stroke: string) => {
    const left = Math.max(a, window[0]), right = Math.min(b, window[1]);
    return right > left ? <rect x={x(left)} y={58} width={Math.max(1, x(right) - x(left))} height={236} fill={fill} stroke={stroke}/> : null;
  };
  const ticks = [a, ...timeTicks(a, b, 694, zone).filter(t => t > a + (b - a) * .05 && t < b - (b - a) * .05), b];
  return <svg className="cal-altitude-svg" viewBox="0 0 780 436" role="img" aria-label="Sun, Cyg A, Cas A and Tau A altitude versus time at OVRO" data-start={iso(a)} data-end={iso(b)} data-altitude-min="0" data-altitude-max="90"
    onMouseMove={event => {const box = event.currentTarget.getBoundingClientRect(); const f = ((event.clientX - box.left) / box.width * 780 - 62) / 694; setCursor(Math.round(Math.max(0, Math.min(1, f)) * (data.times.length - 1)));}}
    onMouseLeave={() => setCursor(null)}>
    <rect width={780} height={436} fill="white"/>
    <g fontFamily="Arial, sans-serif" fontSize={13} fill="#263247">
      {data.tracks.map((track, i) => <g key={track.id}><line x1={64 + i * 175} x2={91 + i * 175} y1={23} y2={23} stroke={COLORS[i]} strokeWidth={2.5} strokeDasharray={DASHES[i]}/><text x={98 + i * 175} y={28}>{track.label}</text></g>)}
      <defs><clipPath id={id}><rect x={62} y={58} width={694} height={236}/></clipPath></defs>
      <g clipPath={`url(#${id})`}>
        {data.windows.map(w => <g key={w.start}>{spans([w.start, w.end], '#dcebe2', 'none')}</g>)}
        {spans(solve.map(unix), '#fbe9c8', '#b46b00')}
        {staticWindow && spans(staticWindow.map(unix), '#dbeafe', '#1468a6')}
        <rect data-low-altitude-band="20" x={62} y={y(20)} width={694} height={y(0) - y(20)} fill="#dce0e5" opacity={.8}/>
        {altitudeTicks.map(v => <line key={v} data-altitude-line={v} x1={62} x2={756} y1={y(v)} y2={y(v)} stroke={v === 20 ? '#64748b' : '#dfe3e8'} strokeWidth={v === 20 ? 1.5 : 1} strokeDasharray={v === 20 ? undefined : '3 3'}/>)}
        {ticks.map(t => <line key={t} x1={x(t)} x2={x(t)} y1={58} y2={294} stroke="#e0e5eb"/>)}
        {data.tracks.map((track, i) => <path key={track.id} data-source={track.id} d={track.altitude_deg.map((v, k) => `${k ? 'L' : 'M'}${x(data.times[k]).toFixed(2)},${y(v).toFixed(2)}`).join(' ')} fill="none" stroke={COLORS[i]} strokeWidth={2.2} strokeDasharray={DASHES[i]}/>)}
        {cursor !== null && <line x1={x(data.times[cursor])} x2={x(data.times[cursor])} y1={58} y2={294} stroke="#334155" strokeDasharray="3 3"/>}
      </g>
      <rect x={62} y={58} width={694} height={236} stroke="#64748b" fill="none"/>
      {altitudeTicks.map(v => <text key={v} x={52} y={y(v) + 4} textAnchor="end">{v}</text>)}
      <text transform="translate(18 176) rotate(-90)" textAnchor="middle">Altitude (°)</text>
      <text x={744} y={y(20) - 5} textAnchor="end" fontSize={11}>20°</text>
      {ticks.map(t => {const wall = wallTime(iso(t), zone); return <g key={t} textAnchor="middle"><text x={x(t)} y={313}>{wall.slice(11)}</text><text x={x(t)} y={330} fontSize={11}>{wall.slice(5, 10)}</text></g>;})}
      <text x={409} y={351} textAnchor="middle">Time ({zone === 'UTC' ? 'UTC' : 'OVRO local · PDT/PST'})</text>
      <text x={62} y={376} fontSize={12}>Gray: below 20° · green: altitude cuts met · amber: solve · blue: static</text>
      <text x={62} y={399} fontSize={12}>{cursor === null ? 'Move across the plot to read source altitudes.' : playbackStamp(data.times[cursor], zone)}</text>
      {cursor !== null && data.tracks.map((t, i) => <text key={t.id} x={62 + i * 175} y={421} fill={COLORS[i]} fontSize={13}>{t.label} {t.altitude_deg[cursor] < 0 ? 'below horizon' : `${t.altitude_deg[cursor].toFixed(1)}°`}</text>)}
    </g>
  </svg>;
}

export default function CalibrationSkyPlanner({date, zone, solve, staticWindow, onSelect}: {
  date: string; zone: PlaybackTimeZone; solve: string[]; staticWindow: string[] | null; onSelect: (value: string[]) => void;
}) {
  const [data, setData] = useState<Plan | null>(null), [error, setError] = useState(''), [zoom, setZoom] = useState(false);
  useEffect(() => {let active = true; setError(''); loadPlan(date).then(result => {if (active) setData(result);}).catch(reason => {if (active) setError(reason.message);}); return () => {active = false;};}, [date]);
  const ready = data?.date === date ? data : null;
  const label = `Calibrator altitudes · ${date} · ${zone === 'UTC' ? 'UTC' : 'OVRO local'}`;
  const selected = staticWindow?.map(unix);
  const covered = ready && selected && selected[0] >= ready.times[0] && selected[1] <= ready.times[ready.times.length - 1] && selected[1] > selected[0];
  const fail = covered ? ready.tracks.filter(track => track.altitude_deg.some((v, i) => ready.times[i] >= selected[0] && ready.times[i] <= selected[1] && v >= track.cap_deg)) : [];
  const plot = ready && <AltitudePlot data={ready} zone={zone} solve={solve} staticWindow={staticWindow}/>;
  return <section className="cal-planner" aria-label="Night-time template planner">
    <div className="cal-section-heading"><h3>Choose a static window</h3><span>{date} · 00:00–23:59 PT</span></div>
    {error ? <p role="alert">Sky guide unavailable: {error}</p> : !ready ? <p>Calculating source altitudes…</p> : <>
      <button className="cal-altitude-trigger" aria-label="Enlarge calibrator altitude plot" onClick={() => setZoom(true)}>{plot}</button>
      <p className="cal-caption">Sun & Tau A below 0° · Cyg A below 20° · Cas A below 15°. Cas A never sets at OVRO.</p>
      {staticWindow && <p className={`cal-planner-selection ${fail.length ? 'attention' : ''}`}>{!covered ? 'Selected static window is outside this plot or has an invalid range.' : fail.length ? `Selected static window exceeds altitude limits: ${fail.map(t => t.label).join(', ')}.` : 'Selected static window meets the sampled altitude limits.'}</p>}
      <div className="cal-window-suggestions">{ready.windows.length ? ready.windows.map(w => <div key={w.start}><span>{playbackStamp(w.start, zone)} → {playbackStamp(w.end, zone)}</span><button onClick={() => onSelect(w.selection.map(iso))}>Use {w.minutes} min</button></div>) : <p>No interval of at least 15 min meets all four altitude limits in this range.</p>}</div>
      <p className="cal-caption">Sky positions only. Check data coverage, RFI and matching hardware settings before using a template.</p>
      {zoom && <FigureZoom title={label} onClose={() => setZoom(false)}>{plot}</FigureZoom>}
    </>}
  </section>;
}
