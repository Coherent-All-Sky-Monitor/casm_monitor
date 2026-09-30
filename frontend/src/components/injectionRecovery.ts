type Counts = Partial<Record<'recovered'|'completed_fired'|'missed_t1'|'missed_t2'|'fire_failed'|'pending'|'unknown', number>>;
type Injections = {status?: string; counts_complete?: boolean; counts?: Counts};
type Health = 'ok'|'late'|'silent'|'unknown'|'yellow'|'orange';
const labels: Record<Health, string> = {
  ok: 'All recovered', yellow: 'Needs attention', orange: 'Reduced recovery',
  silent: 'Low recovery', late: 'Pending / incomplete / stale', unknown: 'No measurement',
};

export function injectionRecovery(injections: Injections|null|undefined, fresh: boolean) {
  const c = injections?.counts ?? {}, failures = c.fire_failed ?? 0;
  let health: Health;
  if (!injections || injections.status === 'unavailable') health = 'unknown';
  else if (!fresh || !injections.counts_complete) health = 'late';
  else if (!((c.completed_fired ?? 0) > 0 || failures > 0))
    health = (c.pending ?? 0) + (c.unknown ?? 0) > 0 ? 'late' : 'unknown';
  // Nonempty, complete recovery takes priority over the partial-recovery bands.
  else if ((c.completed_fired ?? 0) > 0 && c.recovered === c.completed_fired
    && failures === 0 && (c.missed_t1 ?? 0) === 0 && (c.missed_t2 ?? 0) === 0
    && (c.pending ?? 0) === 0 && (c.unknown ?? 0) === 0) health = 'ok';
  else health = (c.recovered ?? 0) < 12 ? 'silent' : (c.recovered ?? 0) <= 17 ? 'orange' : 'yellow';
  return {health, label: labels[health]};
}
