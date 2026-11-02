import assert from 'node:assert/strict';
import { test } from 'node:test';
import { dailyTourPipeline } from '../../scripts/daily-tour-pipeline.js';
import { QuotaStop } from '../../src/lib/budget.js';
import { CONTENT_TYPES } from '../../src/lib/mapping.js';
import type { Store, Place } from '../../src/lib/store.js';
import type { TourSource, Item } from '../../src/lib/tour-api.js';

const quiet = () => {};
const options = { categories: [...CONTENT_TYPES], limit: 10_000, maxSeconds: 60, retryFailed: false, since: '2026-10-01' };
const raw = (id: number) => ({ contentid: String(id), contenttypeid: '12', title: 'Place',
  modifiedtime: '20261102120000', mapx: 127, mapy: 37 });

function memoryStore() {
  const rows = new Map<string, Item>();
  const queue: Place[] = [];
  const events: string[] = [];
  let checkpoint: string | undefined;
  const store: Store = {
    async master(chunk) {
      assert.ok(chunk.length <= 100);
      const counts = { inserted: 0, updated: 0, skipped: 0, deactivated: 0 };
      for (const row of chunk) {
        const id = String(row.content_id);
        if (row.is_active === false) { rows.set(id, row); counts.deactivated++; continue; }
        if (rows.has(id)) { counts.skipped++; continue; }
        rows.set(id, row); counts.inserted++;
        queue.push({ content_id: id, content_type_id: '12', title: 'Place', claim_token: id });
      }
      events.push('master');
      return counts;
    },
    async dailySince(day) { return checkpoint ?? day; },
    async completeDaily(day) { checkpoint = day; events.push('checkpoint'); },
    async claim(limit) { assert.ok(limit <= 100); events.push('claim'); return queue.splice(0, limit); },
    async finish() { events.push('finish'); return true; },
    async release() {},
  };
  return { store, rows, queue, events, checkpoint: () => checkpoint };
}

test('daily pipeline upserts before detail, reflects inactive records and checkpoints only the complete master', async () => {
  const memory = memoryStore();
  const api: TourSource = { async page(endpoint, params) {
    if (endpoint === 'areaBasedList2') return { items: params.contentTypeId === 12 ? [raw(1)] : [], total: params.contentTypeId === 12 ? 1 : 0 };
    if (endpoint === 'areaBasedSyncList2') {
      assert.equal(params.showflag, '0'); assert.match(String(params.modifiedtime), /^\d{8}$/);
      return { items: params.contentTypeId === 12 ? [{ contentid: '2', modifiedtime: '20261102120000' }] : [], total: params.contentTypeId === 12 ? 1 : 0 };
    }
    return { items: [{ contentid: params.contentId }], total: 1 };
  } };
  const result = await dailyTourPipeline(memory.store, api, options, quiet);
  assert.equal(result.completed, 1); assert.equal(result.deferred, false);
  assert.equal(memory.rows.get('2')?.is_active, false);
  assert.ok(memory.events.indexOf('claim') > memory.events.indexOf('checkpoint'));
});

test('master change cap retains checkpoint, drains saved work and replays without starving later changes', async () => {
  const memory = memoryStore();
  const api: TourSource = { async page(endpoint, params) {
    if (endpoint === 'areaBasedList2') return { items: params.contentTypeId === 12 ? [raw(1), raw(2), raw(3)] : [], total: params.contentTypeId === 12 ? 3 : 0 };
    if (endpoint === 'areaBasedSyncList2') return { items: [], total: 0 };
    return { items: [{ contentid: params.contentId }], total: 1 };
  } };
  const first = await dailyTourPipeline(memory.store, api, { ...options, limit: 2 }, quiet);
  assert.equal(first.completed, 2); assert.equal(first.deferred, true);
  assert.equal(memory.rows.size, 2); assert.equal(memory.checkpoint(), undefined);
  const second = await dailyTourPipeline(memory.store, api, { ...options, limit: 2 }, quiet);
  assert.equal(second.completed, 1); assert.equal(second.deferred, false);
  assert.equal(memory.rows.size, 3); assert.ok(memory.checkpoint());
});

test('daily pipeline hard caps enrichment at 10000 even when a caller requests more', async () => {
  const memory = memoryStore();
  memory.queue.push(...Array.from({ length: 10_001 }, (_, i) => ({ content_id: String(i), content_type_id: '12', title: 'Place', claim_token: String(i) })));
  const result = await dailyTourPipeline(memory.store, { async page(endpoint, params) {
    return endpoint.startsWith('areaBased') ? { items: [], total: 0 }
      : { items: [{ contentid: params.contentId }], total: 1 };
  } }, { ...options, limit: 25_000 }, quiet);
  assert.equal(result.limit, 10_000); assert.equal(result.claimed, 10_000);
  assert.equal(result.completed, 10_000); assert.equal(memory.queue.length, 1);
  assert.equal(result.deferred, true); assert.equal(result.stop_reason, 'place_limit');
});

test('quota exhaustion in master does not start enrichment or advance checkpoint', async () => {
  const memory = memoryStore();
  const result = await dailyTourPipeline(memory.store, { async page() { throw new QuotaStop(); } }, options, quiet);
  assert.equal(result.deferred, true); assert.equal(result.claimed, 0);
  assert.equal(memory.checkpoint(), undefined); assert.ok(!memory.events.includes('claim'));
});
