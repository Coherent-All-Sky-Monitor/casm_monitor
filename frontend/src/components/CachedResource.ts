import {Dispatch, SetStateAction, useCallback, useEffect, useRef, useState} from 'react';
import {api, Json} from './Workspace';
import {readCache} from '../lib/readCache';
import {useDelayedBusy} from './RefreshState';

const selections = new Map<string, unknown>();
/** Remember controls when switching tabs, not across reloads or other browsers. */
export function useTabState<T>(key: string, initial: T | (() => T)): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => selections.has(key) ? selections.get(key) as T : typeof initial === 'function' ? (initial as () => T)() : initial);
  const current = useRef(value);
  const update = useCallback<Dispatch<SetStateAction<T>>>(next => {
    current.current = typeof next === 'function' ? (next as (old: T) => T)(current.current) : next;
    selections.set(key, current.current); setValue(current.current);
  }, [key]);
  useEffect(() => { selections.set(key, current.current); }, [key]);
  return [value, update];
}

export const cachedApi = <T = Json>(url: string, maxAge = 30000) => readCache.read(url, () => api<T>(url), maxAge);

/** Restore exact-key plots immediately. Poll only the mounted, visible tab.
 * Health callers use a separate staleAfter deadline; a refresh isn't a failure.
 * In-flight reads are shared, so unmounting a consumer never aborts another's read.
 */
export function useCachedResource<T = Json>(url: string | null, interval = 0,
  {maxAge = interval || 30000, allowStale = true, staleAfter = maxAge, debounce = 0, keepPrevious = false}: {maxAge?: number; allowStale?: boolean; staleAfter?: number; debounce?: number; keepPrevious?: boolean} = {}) {
  const [result, setResult] = useState<{url: string; data: T; at: number} | null>(() => {
    const saved = url ? readCache.peek<T>(url) : undefined;
    return saved && url ? {url, ...saved} : null;
  });
  const [failure, setFailure] = useState<{url: string; message: string} | null>(null);
  const [loading, setLoading] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [,setExpiry]=useState(0);
  const forced = useRef(false);
  const refresh = useCallback(() => { forced.current = true; setRevision(n => n + 1); }, []);
  useEffect(() => {
    if (!url) return;
    let active = true;
    const force = forced.current; forced.current = false;
    const load = async (forceRead = false) => {
      setLoading(url);
      try {
        const data = await cachedApi<T>(url, forceRead ? 0 : maxAge);
        if (active) { setResult({url, data, at: readCache.peek(url)?.at ?? Date.now()}); setFailure(null); }
      } catch (error) {
        if (active) setFailure({url, message: (error as Error).message});
      } finally { if (active) setLoading(null); }
    };
    const start = window.setTimeout(() => void load(force), debounce);
    const timer = interval ? window.setInterval(() => { if (!document.hidden) void load(); }, interval) : 0;
    const visible = () => { if (!document.hidden) void load(); };
    if (interval) document.addEventListener('visibilitychange', visible);
    return () => { active = false; window.clearTimeout(start); window.clearInterval(timer); document.removeEventListener('visibilitychange', visible); };
  }, [url, interval, maxAge, debounce, revision]);
  const cached = url ? readCache.peek<T>(url) : undefined;
  const stored = cached ?? (result?.url === url || keepPrevious ? result : undefined);
  const deadline=stored ? stored.at+staleAfter : 0;
  useEffect(()=>{
    if(allowStale||!deadline||deadline<=Date.now())return;
    // Expire health even if a request hangs or the caller otherwise stops rendering.
    const timer=window.setTimeout(()=>setExpiry(n=>n+1),deadline-Date.now()+1);
    return()=>window.clearTimeout(timer);
  },[allowStale,deadline,url]);
  const expired=!!stored&&!allowStale&&Date.now()>=deadline;
  const usable = stored && !expired;
  const busy=loading===url&&url!==null;
  const updating=useDelayedBusy(busy);
  return {data: usable ? stored.data : null, error: failure?.url === url ? failure.message : url ? readCache.error(url) : '', expired, busy, updating, refresh};
}
