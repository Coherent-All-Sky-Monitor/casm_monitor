import type { Json } from './Workspace';

// Small rendered tiles + curves, never visibility cubes. Bounded per source;
// persistence is best-effort (private browsing/quota fall back to tab memory).
const CACHE = 'casm-transit-figures-v1';
const PREFIX = '/__casm_transit_figures__/';
const memory = new Map<string, Json>();
const pending = new Map<string, Promise<Json>>();
let queue: Promise<unknown> = Promise.resolve();
const keyFor = (source:string, row:Json) => `${PREFIX}${source}/${row.date}/${row.cache_key}`;

async function diskCache() {
  try { return await caches.open(CACHE); } catch { return null; }
}

async function retainNewest(source:string, key:string, body:Json) {
  memory.set(key, body);
  const disk = await diskCache();
  try { await disk?.put(key, new Response(JSON.stringify(body), {headers:{'Content-Type':'application/json'}})); } catch { /* quota: memory still works */ }
  const prefix = `${PREFIX}${source}/`;
  const stored = await disk?.keys().catch(()=>[]) ?? [];
  const keys = [...new Set([...memory.keys(), ...stored.map(r=>new URL(r.url).pathname)])].filter(k=>k.startsWith(prefix));
  // ISO dates sort newest first. Remove an old revision for this date too.
  const dates = [...new Set(keys.map(k=>k.slice(prefix.length).split('/')[0]))].sort().reverse().slice(0,3);
  for (const old of keys) {
    const date = old.slice(prefix.length).split('/')[0];
    if (!dates.includes(date) || date===body.date && old!==key) {
      memory.delete(old);
      try { await disk?.delete(old); } catch { /* unavailable storage */ }
    }
  }
}

export function cachedTransit(source:string, row:Json, calibration:string):Promise<Json> {
  const key = keyFor(source,row);
  if (memory.has(key)) return Promise.resolve(memory.get(key)!);
  if (pending.has(key)) return pending.get(key)!;
  // Source switches do not abort an expensive in-flight GET and then start it
  // over. All sources share one queue and each result is reused on return.
  const request = queue.then(async()=>{
    const disk = await diskCache();
    try {
      const saved = await disk?.match(key);
      if (saved) {
        const body = await saved.json();
        if (body.source===source && body.date===row.date && body.calibration?.id===calibration && body.tile && body.light_curve) {
          memory.set(key,body);
          return body;
        }
      }
    } catch { /* invalid/evicted browser cache: load the selected transit */ }
    const response = await fetch(`/api/sources/transits/${encodeURIComponent(source)}/${row.date}?calibration_id=${calibration}`);
    if (!response.ok) {
      const error = await response.json().catch(()=>null);
      throw new Error(error?.detail ?? `Beam unavailable (HTTP ${response.status})`);
    }
    const body = await response.json();
    await retainNewest(source,key,body);
    return body;
  });
  pending.set(key,request);
  queue = request.catch(()=>undefined);
  void request.finally(()=>pending.delete(key)).catch(()=>undefined);
  return request;
}
