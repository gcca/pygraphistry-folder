import test from 'node:test';
import assert from 'node:assert/strict';
import {parseEnv, sanitize, distances} from './export.mjs';

test('configuration is parsed without evaluating shell expressions', () => {
  const env = parseEnv('export MAPPEDIN_KEY_ID="id"\nMAPPEDIN_KEY=secret\nMAPPEDIN_VENUE=$(touch bad)\n');
  assert.equal(env.MAPPEDIN_VENUE, '$(touch bad)');
  assert.throws(() => parseEnv('MAPPEDIN_KEY_ID=id'));
  assert.throws(() => parseEnv('source another-file'));
});

test('credentials and authentication fields are excluded recursively', () => {
  const value = sanitize({options: {key: 'id', secret: 'credential', accessToken: 'token'},
    metadata: ['prefix-credential', {name: 'Store', phone: ''}]}, ['credential', 'id']);
  assert.deepEqual(value, {options: {}, metadata: [null, {name: 'Store', phone: ''}]});
});

test('all ordered routes preserve zero distance and unreachable status', async () => {
  const targets = [{id: 'ground'}, {id: 'upper'}, {id: 'isolated'}];
  const calls = [];
  const rows = await distances({getDirections: async (a, b) => {
    calls.push([a.id, b.id]);
    if (b.id === 'isolated') return undefined;
    return {distance: a.id === 'ground' ? 0 : 12.345678901234567};
  }}, targets);
  assert.equal(calls.length, 6);
  assert.equal(new Set(calls.map(pair => JSON.stringify(pair))).size, 6);
  assert.equal(rows[0].distance_m, 0);
  assert.equal(rows[1].status, 'unreachable');
  assert.equal(rows[1].distance_m, null);
  assert.equal(rows[2].distance_m, 12.345678901234567);
});

test('routing failures and invalid results abort export', async () => {
  const targets = [{id: 'a'}, {id: 'b'}];
  await assert.rejects(distances({getDirections: async () => {throw new Error('failed');}}, targets));
  for (const distance of [NaN, Infinity, -1]) {
    await assert.rejects(distances({getDirections: async () => ({distance})}, targets));
  }
});
