type RecordValue = Record<string, any>;
export type CalibrationStepState = 'pending' | 'complete' | 'running' | 'failed';

/** Completion means saved outputs, not a scientific pass or runtime activation. */
export function calibrationProgress(detail: RecordValue | null, deployment: RecordValue, reviewed: boolean) {
  const imported = Boolean(detail?.request?.imported);
  const actions: RecordValue[] = detail?.actions ?? [];
  const independent = actions.some(a => a.action === 'transit' && a.selection?.source && a.selection.source !== 'sun');
  const exported = detail?.export;
  const staged = Boolean(exported && deployment?.stage?.checks_ok && deployment.stage.export_id === exported.export_id
    && deployment.stage.export_manifest_sha256 === exported.manifest_sha256);
  const uploaded = (deployment?.uploads??[]).some((row:RecordValue)=>row.state==='done'&&row.exit_code===0);
  const done = [Boolean(detail?.request) || reviewed,
    Boolean(detail?.summary?.paths?.cal_h5) && !imported,
    independent, Boolean(detail?.summary?.has_weights) && !imported,
    staged && !imported];
  const labels = [detail?.request ? 'Recipe saved' : reviewed ? 'Selection reviewed' : 'Windows & membership',
    imported ? 'Recorded reference' : done[1] ? 'Calibration saved' : 'SVD & phase progression',
    independent ? 'Transit saved · review results' : 'Independent-source transit',
    done[3] ? '512 beams + IB saved' : '512-point sky view',
    uploaded ? 'Upload completed · check activation' : done[4] ? 'Dry run passed · not uploaded' : exported ? 'Weights exported · review next' : 'Write weights · test · review'];
  const jobs: RecordValue[] = detail?.jobs ?? [];
  return done.map((complete, index) => {
    const kinds = [[], ['solve', 'inspect', 'phase', 'history'], ['transit'], ['grid'], ['export','deploy_stage','deploy_upload']][index];
    const latest = jobs.filter(j => kinds.includes(j.params?.action ?? j.kind)).sort((a, b) => b.id - a.id)[0];
    const state: CalibrationStepState = latest?.state === 'running' || latest?.state === 'queued' ? 'running'
      : latest?.state === 'failed' || latest?.state === 'cancelled' ? 'failed' : complete ? 'complete' : 'pending';
    return {state, label: state === 'running' ? 'Working…' : state === 'failed' ? 'Needs attention · view log' : labels[index]};
  });
}
