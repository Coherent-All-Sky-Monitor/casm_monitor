import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

const source = readFileSync(new URL('../src/components/timeZones.ts', import.meta.url), 'utf8');
const js = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020}}).outputText;
const {LOCAL, wallTime, utcFromWall} = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));

test('OVRO summer/winter dates convert independently of the browser zone', () => {
  assert.equal(wallTime('2026-09-18T19:47:00Z', LOCAL), '2026-09-18T12:47');
  assert.equal(wallTime('2026-09-18T03:00:00Z', LOCAL), '2026-09-17T20:00');
  assert.equal(wallTime('2026-12-18T19:47:00Z', LOCAL), '2026-12-18T11:47');
  for (const utc of ['2026-09-18T03:00', '2026-09-18T19:47', '2026-12-18T19:47']) {
    for (const zone of [LOCAL, 'UTC']) assert.equal(utcFromWall(wallTime(utc, zone), zone, true), utc);
  }
});
test('UTC parsing accepts canonical and explicitly offset API timestamps', () => {
  for (const utc of ['2026-09-18 19:47:00', '2026-09-18T19:47:00Z', '2026-09-18T19:47:00+00:00', '2026-09-18T12:47:00-07:00']) {
    assert.equal(wallTime(utc, LOCAL), '2026-09-18T12:47');
  }
  assert.equal(wallTime('', LOCAL), '');
  assert.equal(wallTime('invalid', LOCAL), '');
});
test('invalid and nonexistent local times cannot become solve timestamps', () => {
  for (const value of ['', 'invalid', '2026-02-30T10:00', '2026-10-01T24:00', '2026-10-01T12:00:30']) {
    assert.throws(() => utcFromWall(value, LOCAL, true), /complete, valid/);
  }
  assert.throws(() => utcFromWall('2026-03-08T02:30', LOCAL, true), /does not exist/);
  assert.equal(utcFromWall('2026-03-08T01:59', LOCAL, true), '2026-03-08T09:59');
  assert.equal(utcFromWall('2026-03-08T03:00', LOCAL, true), '2026-03-08T10:00');
});
test('calibration rejects a repeated local hour; UTC chooses either occurrence', () => {
  assert.throws(() => utcFromWall('2026-11-01T01:30', LOCAL, true), /occurs twice/);
  for (const utc of ['2026-11-01T08:30', '2026-11-01T09:30']) {
    assert.equal(wallTime(utc, LOCAL), '2026-11-01T01:30');
    assert.equal(utcFromWall(utc, 'UTC', true), utc);
  }
  assert.equal(utcFromWall('2026-11-01T01:30', LOCAL), '2026-11-01T08:30');
});
