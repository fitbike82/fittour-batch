import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { optionalSnapshot } from '../../src/lib/snapshot.js';
import { BatchError } from '../../src/lib/budget.js';
import type { Snapshot } from '../../src/lib/store.js';

test('optional snapshots warn safely on timeout or network errors and preserve successful snapshots', async () => {
  const events: object[] = [];
  for (const error of [new BatchError('database_snapshot_57014'), new TypeError('secret URL')]) {
    const store = { async snapshot() { throw error; } };
    assert.equal(await optionalSnapshot(store, 'after', event => events.push(event)), undefined);
  }
  assert.deepEqual(events, [
    { event: 'snapshot_skipped', phase: 'after', reason: 'database_snapshot_57014' },
    { event: 'snapshot_skipped', phase: 'after', reason: 'TypeError' },
  ]);
  const snapshot = { database_total: 25_000 } as Snapshot;
  assert.equal(await optionalSnapshot({ async snapshot() { return snapshot; } }, 'before', () => {}), snapshot);
});

test('CLI completes 25000 places despite both snapshot timeouts and emits 250 lightweight progress logs', () => {
  const folder = mkdtempSync(join(tmpdir(), 'snapshot-batch-'));
  try {
    const reportPath = join(folder, 'report.json'), summaryPath = join(folder, 'summary.md');
    const fixture = fileURLToPath(new URL('./fixtures/snapshot-run.ts', import.meta.url));
    const tsx = import.meta.resolve('tsx');
    const stdout = execFileSync(process.execPath, ['--import', tsx, fixture,
      '--limit', '25000', '--categories', '12', '--max-seconds', '60', '--report', reportPath], {
      cwd: folder, encoding: 'utf8', maxBuffer: 2 * 1024 * 1024,
      env: { ...process.env, SUPABASE_URL: 'https://example.supabase.co',
        SUPABASE_SERVICE_ROLE_KEY: 'test-key', TOUR_API_DECODING_KEY: 'test-tour-key',
        TEST_PLACE_LIMIT: '25000', TEST_JOB_RESULT: '', TEST_SNAPSHOT_ERROR: '57014', GITHUB_STEP_SUMMARY: summaryPath },
    });
    const events = stdout.trim().split(/\r?\n/).map(line => JSON.parse(line));
    const progress = events.filter(event => event.event === 'enrichment_progress');
    assert.equal(progress.length, 250);
    assert.ok(progress.every(event => !('database' in event)));
    assert.equal(progress.at(-1).api_calls, 50_000);
    assert.equal(progress.at(-1).completed, 25_000);
    assert.deepEqual(events.filter(event => event.event === 'snapshot_skipped').map(event => event.phase), ['before', 'after']);
    assert.ok(!events.some(event => event.event === 'batch_stopped'));
    assert.deepEqual(events.at(-1), { event: 'test_totals', snapshots: 2, saved: 25_000 });
    const report = JSON.parse(readFileSync(reportPath, 'utf8'));
    assert.equal(report.status, 'completed');
    assert.equal(report.result.completed, 25_000);
    assert.equal(report.result.snapshot_calls_during_job, 1);
    assert.equal(report.code, undefined);
    assert.equal(report.before, undefined); assert.equal(report.after, undefined);
    assert.match(readFileSync(summaryPath, 'utf8'), /FitTour enrichment: completed/);
  } finally { rmSync(folder, { recursive: true, force: true }); }
});

test('snapshot network failures preserve completed, deferred and fatal job reports and exit codes', () => {
  for (const [mode, status, exitCode] of [['', 'completed', 0], ['deferred', 'deferred', 2], ['fatal', 'failed', 1]] as const) {
    const folder = mkdtempSync(join(tmpdir(), 'snapshot-network-'));
    try {
      const reportPath = join(folder, 'report.json');
      const child = spawnSync(process.execPath, ['--import', import.meta.resolve('tsx'),
        fileURLToPath(new URL('./fixtures/snapshot-run.ts', import.meta.url)),
        '--limit', '1', '--categories', '12', '--report', reportPath], {
        cwd: folder, encoding: 'utf8', env: { ...process.env,
          SUPABASE_URL: 'https://example.supabase.co', SUPABASE_SERVICE_ROLE_KEY: 'test-key',
          TOUR_API_DECODING_KEY: 'test-tour-key', TEST_PLACE_LIMIT: '1', TEST_SNAPSHOT_ERROR: 'network',
          TEST_JOB_RESULT: mode, GITHUB_STEP_SUMMARY: join(folder, 'summary.md') },
      });
      assert.equal(child.status, exitCode, child.stderr);
      const report = JSON.parse(readFileSync(reportPath, 'utf8'));
      assert.equal(report.status, status);
      assert.equal(report.code, mode === 'fatal' ? 'database_claim_08006' : undefined);
      const events = child.stdout.trim().split(/\r?\n/).map(line => JSON.parse(line));
      assert.equal(events.filter(event => event.event === 'snapshot_skipped').length, 2);
      assert.deepEqual(events.at(-1), { event: 'test_totals', snapshots: 2, saved: 1 });
    } finally { rmSync(folder, { recursive: true, force: true }); }
  }
});
