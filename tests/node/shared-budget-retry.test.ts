import test from 'node:test';
import assert from 'node:assert/strict';
import type { SupabaseClient } from '@supabase/supabase-js';
import { SharedBudget } from '../../src/lib/store.js';

test('quota reservation retries transport errors conservatively and reserves locally once', async () => {
  let calls = 0, localCalls = 0;
  const waits: number[] = [];
  const db = { async rpc() {
    calls++;
    return calls < 3 ? { error: { code: '' }, status: 503 } : { data: true };
  } } as unknown as SupabaseClient;
  const budget = new SharedBudget(db, 'key', {
    async reserve() { localCalls++; }, async block() {},
  }, 100000, 0, async ms => { waits.push(ms); });
  await budget.reserve();
  assert.equal(calls, 3);
  assert.equal(localCalls, 1);
  assert.deepEqual(waits, [500, 1000]);
});

test('quota database failures stay distinct from quota exhaustion and stop after bounded retries', async () => {
  for (const code of ['', '42501']) {
    let calls = 0;
    const db = { async rpc() { calls++; return { error: { code }, status: 0 }; } } as unknown as SupabaseClient;
    const budget = new SharedBudget(db, 'key', { async reserve() {}, async block() {} },
      100000, 0, async () => {});
    await assert.rejects(budget.reserve(), new RegExp(`database_quota_${code || 'transport'}`));
    assert.equal(calls, code ? 1 : 4);
  }
});
