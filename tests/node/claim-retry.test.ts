import assert from 'node:assert/strict';
import { test } from 'node:test';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { BatchError } from '../../src/lib/budget.js';
import { enrich } from '../../src/jobs/enrichment.js';
import { SupabaseStore, type Store } from '../../src/lib/store.js';
import type { SupabaseClient } from '@supabase/supabase-js';

const options = { limit: 200, retryFailed: false, maxSeconds: 60, categories: [12] };
const row = (id: string) => ({ content_id: id, content_type_id: '12', title: 'Place', claim_token: id });
const api = { async page(_endpoint: string, params: Record<string, string | number>) {
  return { items: [{ contentid: params.contentId }], total: 1 };
} };

test('claim recovers after three retries with unchanged cursor, category and batch limit', async () => {
  for (const error of [new BatchError('database_claim_57014'), new TypeError('private network URL'),
    new BatchError('database_claim_transport'), new BatchError('database_claim_08006')]) {
    const waits: number[] = [], events: object[] = [], calls: unknown[][] = [];
    let attempts = 0, saved = 0;
    const store = {
      async claim(...args: unknown[]) {
        calls.push(args);
        if (attempts++ === 0) return [row('1')];
        if (attempts <= 4) throw error;
        return attempts === 5 ? [row('2')] : [];
      },
      async finish() { saved++; return true; }, async release() { assert.fail('no leases to release'); },
    } as unknown as Store;
    const result = await enrich(store, api, options, event => events.push(event), async ms => { waits.push(ms); });
    assert.deepEqual(result, { claimed: 2, completed: 2, failed: 0, deferred: false });
    assert.equal(saved, 2);
    assert.deepEqual(waits, [3000, 7000, 15000]);
    assert.ok(calls.slice(1, 5).every(args => JSON.stringify(args) === JSON.stringify([100, '1', false, [12]])));
    assert.equal(events.filter(event => JSON.stringify(event).includes('claim_retry')).length, 3);
    assert.ok(!JSON.stringify(events).includes('private network URL'));
  }
});

test('claim retry exhaustion preserves saved counts and defers without advancing to another category', async () => {
  let calls = 0, saved = 0;
  const waits: number[] = [];
  const store = {
    async claim() { if (++calls === 1) return [row('1')]; throw new BatchError('database_claim_57014'); },
    async finish() { saved++; return true; }, async release() { assert.fail(); },
  } as unknown as Store;
  const result = await enrich(store, api, { ...options, categories: [12, 39] }, () => {}, async ms => { waits.push(ms); });
  assert.deepEqual(result, { claimed: 1, completed: 1, failed: 0, deferred: true, stop_reason: 'database_claim_57014' });
  assert.equal(calls, 5); assert.equal(saved, 1);
  assert.deepEqual(waits, [3000, 7000, 15000]);
});

test('claim retries do not conceal permanent errors or exceed the runtime boundary', async () => {
  let calls = 0;
  const store = { async claim() { calls++; throw new BatchError('database_claim_42501'); } } as unknown as Store;
  await assert.rejects(enrich(store, api, options, () => {}, async () => assert.fail()), /database_claim_42501/);
  assert.equal(calls, 1);
  store.claim = async () => { throw new BatchError('database_claim_57014'); };
  const result = await enrich(store, api, { ...options, maxSeconds: 1 }, () => {}, async () => assert.fail());
  assert.equal(result.deferred, true); assert.equal(result.claimed, 0);
});

test('Supabase claim normalizes transport responses without retrying permission errors', async () => {
  for (const [status, code, expected] of [[0, '', 'transport'], [503, '', 'transport'],
    [500, '57014', '57014'], [403, '42501', '42501']] as const) {
    const db = { async rpc() { return { data: null, error: { code }, status }; } } as unknown as SupabaseClient;
    await assert.rejects(new SupabaseStore(db).claim(100, '', false, [12]), new RegExp(`database_claim_${expected}`));
  }
});

test('CLI publishes saved results with deferred exit code after claim retries are exhausted', () => {
  const folder = mkdtempSync(join(tmpdir(), 'claim-exhausted-'));
  try {
    const reportPath = join(folder, 'report.json');
    const child = spawnSync(process.execPath, ['--import', import.meta.resolve('tsx'),
      fileURLToPath(new URL('./fixtures/snapshot-run.ts', import.meta.url)),
      '--limit', '101', '--categories', '12', '--report', reportPath], {
      cwd: folder, encoding: 'utf8', env: { ...process.env,
        SUPABASE_URL: 'https://example.supabase.co', SUPABASE_SERVICE_ROLE_KEY: 'test-key',
        TOUR_API_DECODING_KEY: 'test-tour-key', TEST_PLACE_LIMIT: '101', TEST_CLAIM_ERROR: '57014',
        TEST_JOB_RESULT: '', TEST_SNAPSHOT_ERROR: '57014', GITHUB_STEP_SUMMARY: join(folder, 'summary.md') },
    });
    assert.equal(child.status, 2, child.stderr);
    const report = JSON.parse(readFileSync(reportPath, 'utf8'));
    assert.equal(report.status, 'deferred');
    assert.deepEqual(report.result, { claimed: 100, completed: 100, failed: 0, deferred: true,
      stop_reason: 'database_claim_57014', snapshot_calls_during_job: 1 });
    assert.equal(report.code, undefined);
    const events = child.stdout.trim().split(/\r?\n/).map(line => JSON.parse(line));
    assert.equal(events.filter(event => event.event === 'claim_retry').length, 3);
    assert.ok(!events.some(event => event.event === 'batch_stopped'));
    assert.deepEqual(events.at(-1), { event: 'test_totals', snapshots: 2, saved: 100 });
  } finally { rmSync(folder, { recursive: true, force: true }); }
});
