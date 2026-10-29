import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { BatchError, DailyBudget, QuotaStop, kstDay, type Budget } from '../../src/lib/budget.js';
import { TourApi, type Item, type TourSource } from '../../src/lib/tour-api.js';
import { CONTENT_TYPES, courseStops, mapMaster, sanitizeIntro } from '../../src/lib/mapping.js';
import type { Counts, Details, Place, Store } from '../../src/lib/store.js';
import { syncMaster } from '../../scripts/sync-master-catalog.js';
import { enrich } from '../../src/jobs/enrichment.js';

const raw = (id = '1', category = 12): Item => ({ contentid: id, contenttypeid: String(category),
  title: 'Place', modifiedtime: '20261028120000', mapx: '127', mapy: '37.5' });
const place = (id = '1', category = '12'): Place => ({ content_id: id, content_type_id: category, title: 'Place', claim_token: id });
const ok = (items: Item[], total = items.length) => ({ items, total });
const quiet = () => {};
class MemoryStore implements Store {
  rows: Item[] = []; saved: Array<{ place: Place; details: Details }> = [];
  released: Array<{ place: Place; failed: boolean }> = []; chunks: number[] = [];
  constructor(public queue: Place[] = []) {}
  async master(rows: Item[]): Promise<Counts> { this.rows.push(...rows); this.chunks.push(rows.length);
    return { inserted: rows.length, updated: 0, skipped: 0, deactivated: 0 }; }
  async claim(limit: number) { return this.queue.splice(0, limit); }
  async finish(place: Place, details: Details) { this.saved.push({ place, details }); return true; }
  async release(place: Place, failed: boolean) { this.released.push({ place, failed }); }
}
class MemoryBudget implements Budget {
  calls = 0; blocked = false;
  reserve() { if (this.blocked) throw new QuotaStop(); this.calls++; }
  block() { this.blocked = true; }
}
const json = (code = '0000') => new Response(JSON.stringify({ response: { header: { resultCode: code },
  body: { items: { item: [{ contentid: '1' }] }, totalCount: 1 } } }));

test('master mapper enforces Korea bounds, dates, lengths, and course identity', () => {
  const row = mapMaster({ ...raw('123', 25), title: '가'.repeat(300), addr1: 'x'.repeat(500) }, 25);
  assert.equal(row.content_type_id, '25'); assert.equal(row.title?.toString().length, 255);
  assert.equal(row.addr1?.toString().length, 120);
  for (const value of [null, '', 0, 'bad', NaN, Infinity, 123, 133]) {
    assert.throws(() => mapMaster({ ...raw(), mapx: value }, 12), /invalid_coordinates/);
  }
  assert.equal(mapMaster({ ...raw(), mapy: '39' }, 12).mapy, 39);
  assert.throws(() => mapMaster({ ...raw(), modifiedtime: '20260230120000' }, 12));
  assert.deepEqual(sanitizeIntro({ eventstartdate: '20260230', eventenddate: '20240229', field: 'a\0b' }),
    { eventstartdate: null, eventenddate: '20240229', field: 'ab' });
});

test('full catalog walks all eight types with 1000-row paging and 100-row writes', async () => {
  const store = new MemoryStore(); const calls: Array<Record<string, string | number>> = [];
  const api: TourSource = { async page(endpoint, params) {
    assert.equal(endpoint, 'areaBasedList2'); assert.equal(params.numOfRows, 1000); calls.push(params);
    if (params.contentTypeId === 12) return Number(params.pageNo) === 1
      ? ok(Array.from({ length: 1000 }, (_, i) => raw(String(i + 1))), 1002)
      : ok([raw('1001'), { ...raw('1002'), mapx: 'bad' }], 1002);
    return params.contentTypeId === 25 ? ok([raw('2000', 25)]) : ok([]);
  } };
  const counts = await syncMaster(store, api, quiet);
  assert.equal(counts.categories, 8); assert.equal(counts.fetched, 1003);
  assert.equal(counts.inserted, 1002); assert.equal(counts.skipped, 1);
  assert.deepEqual([...new Set(calls.map(c => c.contentTypeId))], [...CONTENT_TYPES]);
  assert.ok(store.chunks.every(size => size <= 100));
  assert.equal(store.rows.at(-1)?.content_type_id, '25');
});

test('catalog rejects repeated pages and changing population', async () => {
  for (const change of [false, true]) {
    let page = 0;
    await assert.rejects(syncMaster(new MemoryStore(), { async page() {
      return ok([raw('1')], ++page === 1 ? 2 : change ? 3 : 2);
    } }, quiet), change ? /population_changed/ : /repeated_record/);
  }
});

test('p-limit caps actual concurrent HTTP requests at ten', async () => {
  let active = 0, peak = 0; const budget = new MemoryBudget();
  const api = new TourApi('secret', budget, async () => {
    active++; peak = Math.max(peak, active);
    await new Promise(resolve => setTimeout(resolve, 3)); active--; return json();
  });
  await Promise.all(Array.from({ length: 35 }, () => api.page('detailCommon2', { contentId: '1' })));
  assert.equal(peak, 10); assert.equal(budget.calls, 35);
});

test('retries charge every attempt, credentials never appear in surfaced errors', async () => {
  const budget = new MemoryBudget(); let count = 0;
  const api = new TourApi('secret-key', budget, async () => ++count === 1 ? new Response('', { status: 503 }) : json(), async () => {});
  await api.page('detailCommon2', {}); assert.equal(budget.calls, 2);
  const bad = new TourApi('secret-key', budget, async () => new Response('secret-key', { status: 400 }));
  await assert.rejects(bad.page('detailCommon2', {}), /^Error: http_400$/);
});

test('provider quota blocks queued requests and malformed pages cannot complete', async () => {
  const budget = new MemoryBudget(); const api = new TourApi('key', budget, async () => json('22'));
  await assert.rejects(api.page('detailCommon2', {}), QuotaStop);
  assert.equal(budget.blocked, true);
  await assert.rejects(api.page('detailCommon2', {}), QuotaStop);
  assert.equal(budget.calls, 1);
  const broken = new TourApi('key', new MemoryBudget(), async () => new Response(JSON.stringify({ response: {
    header: { resultCode: '0000' }, body: { totalCount: 1, items: '' } } })));
  await assert.rejects(broken.page('detailCommon2', {}), /incomplete_page/);
});

test('quota journal persists, rejects parallel runs, and uses the KST boundary', () => {
  const dir = mkdtempSync(join(tmpdir(), 'fittour-'));
  try {
    const a = new DailyBudget('key', 2, dir);
    assert.throws(() => new DailyBudget('key', 2, dir), /another_tour_batch/);
    a.reserve(); a.close();
    const b = new DailyBudget('key', 2, dir); b.reserve(); assert.throws(() => b.reserve(), QuotaStop); b.close();
    assert.equal(kstDay(new Date('2026-10-28T14:59:59Z')), '2026-10-28');
    assert.equal(kstDay(new Date('2026-10-28T15:00:00Z')), '2026-10-29');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('course enrichment gathers every page, sorts stops, and writes completion once', async () => {
  const store = new MemoryStore([place('25', '25')]);
  const api: TourSource = { async page(endpoint, params) {
    if (endpoint === 'detailCommon2') return ok([{ contentid: '25', title: 'Course', overview: 'Overview' }]);
    if (endpoint === 'detailIntro2') return ok([{ contentid: '25', distance: '3km', taketime: '1h', schedule: 'day' }]);
    return Number(params.pageNo) === 1 ? ok([{ subnum: '2', subname: 'Last' }, { subnum: '0', subname: 'First' }], 3)
      : ok([{ subnum: '1', subname: 'Middle' }], 3);
  } };
  const result = await enrich(store, api, undefined, quiet);
  assert.equal(result.completed, 1); assert.equal(store.saved.length, 1);
  assert.deepEqual(store.saved[0].details.stops.map(s => s.subname), ['First', 'Middle', 'Last']);
  assert.deepEqual(store.saved[0].details.stops.map(s => s.subnum), [1, 2, 3]);
  assert.equal(store.saved[0].details.course?.distance, '3km');
  assert.throws(() => courseStops([{ subnum: '0', subname: 'a' }, { subnum: '0', subname: 'b' }]), /invalid_course_stop/);
});

test('bad detail fails only its place and quota deferral releases every remaining claim', async () => {
  const store = new MemoryStore([place('1'), place('2')]);
  const api: TourSource = { async page(endpoint, params) {
    if (params.contentId === '1') throw new BatchError('invalid_json_response');
    return ok([{ contentid: '2', overview: 'good' }]);
  } };
  const result = await enrich(store, api, undefined, quiet);
  assert.equal(result.failed, 1); assert.equal(result.completed, 1);
  assert.equal(store.released[0].failed, true);
  const deferred = new MemoryStore(Array.from({ length: 20 }, (_, i) => place(String(i))));
  const stopped = await enrich(deferred, { async page() { throw new QuotaStop(); } }, undefined, quiet);
  assert.equal(stopped.deferred, true); assert.equal(stopped.failed, 0);
  assert.equal(deferred.released.length, 20); assert.equal(deferred.saved.length, 0);
});

test('100-place batch drains after an unexpected error for 1214860', async () => {
  const store = new MemoryStore([place('1214860'), ...Array.from({ length: 104 }, (_, i) => place(String(i + 1)))]);
  const events: object[] = [];
  const result = await enrich(store, { async page(_endpoint, params) {
    if (params.contentId === '1214860') throw new TypeError('unexpected provider structure');
    return ok([{ contentid: params.contentId, overview: 'valid\0text' }]);
  } }, { limit: 100, retryFailed: false, maxSeconds: 60, categories: [39] }, event => events.push(event));
  assert.deepEqual(result, { claimed: 100, completed: 99, failed: 1, deferred: false });
  assert.deepEqual(store.released, [{ place: place('1214860'), failed: true }]);
  assert.equal(store.queue.length, 5);
  assert.ok(store.saved.every(row => row.details.overview === 'validtext'));
  assert.ok(events.some(event => JSON.stringify(event) === JSON.stringify({ event: 'enrichment_error',
    content_id: '1214860', code: 'unexpected_error', stage: 'fetch', error_type: 'TypeError' })));
});

test('null detail is isolated and subsequent claim pages continue', async () => {
  const store = new MemoryStore(Array.from({ length: 101 }, (_, i) => place(String(i + 1))));
  const result = await enrich(store, { async page(_endpoint, params) {
    return params.contentId === '1' ? ok([null as unknown as Item]) : ok([{ contentid: params.contentId }]);
  } }, { limit: 101, retryFailed: false, maxSeconds: 60, categories: [39] }, quiet);
  assert.deepEqual(result, { claimed: 101, completed: 100, failed: 1, deferred: false });
  assert.equal(store.saved.at(-1)?.place.content_id, '101');
  assert.deepEqual(store.released, [{ place: place('1'), failed: true }]);
});

import { SupabaseStore } from '../../src/lib/store.js';
import type { SupabaseClient } from '@supabase/supabase-js';
test('database data rejection isolates one row while schema errors stop immediately', async () => {
  let calls = 0;
  const db = { async rpc(_name: string, params: { p_rows: Item[] }) {
    calls++;
    const rejected = params.p_rows.some(row => row.content_id === '2');
    return rejected ? { data: null, error: { code: '22001' } }
      : { data: { inserted: params.p_rows.length, updated: 0, skipped: 0, deactivated: 0 }, error: null };
  } } as unknown as SupabaseClient;
  const store = new SupabaseStore(db, quiet);
  const counts = await store.master([mapMaster(raw('1'), 12), mapMaster(raw('2'), 12), mapMaster(raw('3'), 12)]);
  assert.equal(counts.inserted, 2); assert.equal(counts.skipped, 1); assert.ok(calls > 1);
  calls = 0;
  const missing = new SupabaseStore({ async rpc() { calls++; return { error: { code: 'PGRST202' } }; } } as unknown as SupabaseClient, quiet);
  await assert.rejects(missing.master([raw('1'), raw('2')]), /database_PGRST202/);
  assert.equal(calls, 1);
});

test('database outage and stale claims release work and fail the run', async () => {
  const api: TourSource = { async page(_endpoint, params) { return ok([{ contentid: params.contentId }]); } };
  const store = new MemoryStore([place('1')]);
  store.finish = async () => { throw new BatchError('database_complete_08006'); };
  await assert.rejects(enrich(store, api, undefined, quiet), /database_complete_08006/);
  assert.equal(store.released[0].failed, false);
  const stale = new MemoryStore([place('2')]); stale.finish = async () => false;
  await assert.rejects(enrich(stale, api, undefined, quiet), /claim_expired/);
  assert.equal(stale.released[0].failed, false);
});


test('detail SQL data errors remain per-place failures while exhausted network retries stop', async () => {
  const db = { async rpc() { return { data: null, error: { code: '22001' } }; } } as unknown as SupabaseClient;
  await assert.rejects(new SupabaseStore(db, quiet).finish(place(), { overview: null, intro: {}, course: null, stops: [] }), /detail_rejected_22001/);
  const store = new MemoryStore([place('1')]);
  await assert.rejects(enrich(store, { async page() { throw new BatchError('retryable_transport'); } }, undefined, quiet), /retryable_transport/);
  assert.equal(store.released[0].failed, false);
});
