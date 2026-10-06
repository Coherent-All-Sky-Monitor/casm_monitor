import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
const source = readFileSync(new URL('../src/components/calibrationProgress.ts', import.meta.url), 'utf8');
const js = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020}}).outputText;
const {calibrationProgress} = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));
test('untouched stays gray; human review completes prepare only', () => {
  assert.deepEqual(calibrationProgress(null, {}, false).map(s => s.state), Array(5).fill('pending'));
  assert.deepEqual(calibrationProgress(null, {}, true).map(s => s.state), ['complete', ...Array(4).fill('pending')]);
});
test('green requires saved outputs; scientific validity and upload are not implied', () => {
  const detail = {request: {}, export: {export_id: 'one'}, summary: {paths: {cal_h5: 'cal.h5'}, has_weights: true}, actions: [{action: 'transit', selection: {source: 'cyg-a'}}]};
  const result = calibrationProgress(detail, {stage: {checks_ok: true, export_id: 'one'}}, false);
  assert(result.every(s => s.state === 'complete'));
  assert.match(result[2].label, /review results/);
  assert.match(result[4].label, /not uploaded/);
  assert.equal(calibrationProgress(detail, {stage: {checks_ok: true, export_id: 'old'}}, false)[4].state, 'pending');
  assert.equal(calibrationProgress({...detail, actions: [{action: 'transit', selection: {source: 'sun'}}]}, {}, false)[2].state, 'pending');
  assert.equal(calibrationProgress({...detail, request: {imported: true}}, {}, false)[1].state, 'pending');
});
test('latest failed or running job takes precedence over old saved results', () => {
  const detail = {request: {}, summary: {paths: {cal_h5: 'cal.h5'}}, jobs: [
    {id: 3, state: 'failed', params: {action: 'phase'}}, {id: 1, state: 'done', params: {action: 'solve'}}]};
  assert.equal(calibrationProgress(detail, {}, false)[1].state, 'failed');
  detail.jobs.push({id: 4, state: 'queued', params: {action: 'phase'}});
  assert.equal(calibrationProgress(detail, {}, false)[1].state, 'running');
});
