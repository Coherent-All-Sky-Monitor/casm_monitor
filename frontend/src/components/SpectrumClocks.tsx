import {playbackStamp} from './snapPlaybackModel';
import '../spectrum-clocks.css';

/** Every clock describes this saved frame, never the browser's current time. */
export function SpectrumClocks({at,lst,dark=false}:{at:number|null|undefined;lst?:string|null;dark?:boolean}){
  if(at==null)return <div className="snap-clock-missing">No saved timestamp</div>;
  const iso=new Date(at*1000).toISOString();
  return <div className={`snap-clock-row${dark?' snap-clock-dark':''}`} aria-label="Saved spectrum times" data-timestamp={at}>
    <div><span>UTC</span><time dateTime={iso} data-clock="utc">{playbackStamp(at,'UTC').replace(' UTC','')}</time></div>
    <div><span>OVRO local</span><time dateTime={iso} data-clock="local">{playbackStamp(at,'America/Los_Angeles')}</time></div>
    <div title="Mean local sidereal time at OVRO, using offline Earth-orientation tables"><span>LST</span><time data-clock="lst">{lst??'Unavailable'}</time></div>
  </div>;
}
