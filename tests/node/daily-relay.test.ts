import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { PGlite } from '@electric-sql/pglite';
import { parse } from 'yaml';
import { syncDailyMaster } from '../../scripts/sync-daily-master.js';
import { enrich } from '../../src/jobs/enrichment.js';
import { options } from '../../src/lib/cli.js';
import { QuotaStop } from '../../src/lib/budget.js';
import { summary } from '../../src/lib/report.js';
import type { Store, Place, Counts, Details, Snapshot } from '../../src/lib/store.js';
import type { Item, TourSource } from '../../src/lib/tour-api.js';
const read = (path: string) => readFileSync(new URL(`../../${path}`, import.meta.url), 'utf8').replace(/^\uFEFF/, '');
const quiet = () => {};
class DailyStore implements Store {
  rows: Item[] = []; checkpoint?: string; defaultDay?: string;
  async master(rows: Item[]): Promise<Counts> { this.rows.push(...rows); return { inserted: rows.length, updated: 0, skipped: 0, deactivated: 0 }; }
  async claim(): Promise<Place[]> { return []; }
  async finish(_place: Place, _details: Details) { return true; }
  async release() {}
  async dailySince(day: string) { this.defaultDay = day; return '2026-10-24'; }
  async completeDaily(day: string) { this.checkpoint = day; }
}
const raw = (id: string, date: string) => ({ contentid: id, contenttypeid: '12', title: 'Place', modifiedtime: `${date}120000`, mapx: 127, mapy: 37 });
test('daily master scans beyond old rows and catches up from checkpoint without advancing on quota stop', async () => {
  const store = new DailyStore(); const pages: number[] = [];
  const api: TourSource = { async page(_endpoint, params) {
    assert.equal(params.arrange, 'C'); assert.equal(params.numOfRows, 1000);
    assert.equal(params.modifiedtime, undefined); // Not an areaBasedList2 parameter.
    if (params.contentTypeId !== 12) return { items: [], total: 0 };
    pages.push(Number(params.pageNo));
    return Number(params.pageNo) === 1
      ? { items: [raw('1', '20261023')], total: 2 }
      : { items: [raw('2', '20261025')], total: 2 };
  } };
  const result = await syncDailyMaster(store, api, { now: new Date('2026-10-27T16:00:00Z') }, quiet);
  assert.equal(store.defaultDay, '2026-10-27'); assert.equal(result.since, '2026-10-24');
  assert.equal(store.checkpoint, '2026-10-28'); assert.equal(result.unchanged, 1);
  assert.deepEqual(store.rows.map(row => row.content_id), ['2']); assert.deepEqual(pages, [1, 2]);
  const deferred = new DailyStore();
  assert.equal((await syncDailyMaster(deferred, { async page() { throw new QuotaStop(); } }, {}, quiet)).deferred, true);
  assert.equal(deferred.checkpoint, undefined);
  const partial = new DailyStore();
  await syncDailyMaster(partial, { async page() { return { items: [], total: 0 }; } }, { categories: [12] }, quiet);
  assert.equal(partial.checkpoint, undefined);
});

test('category priority resets the keyset cursor before scanning each next category', async () => {
  const selected: Array<{ after: string; category: number }> = [];
  const store = new DailyStore();
  store.claim = async (_limit?: number, after?: string, _retry?: boolean, categories?: number[]) => {
    selected.push({ after: after!, category: categories![0] });
    return after ? [] : [{ content_id: categories![0] === 39 ? '900' : '100',
      content_type_id: String(categories![0]), title: 'Place', claim_token: 'token' }];
  };
  const api: TourSource = { async page(_endpoint, params) {
    return { items: [{ contentid: params.contentId }], total: 1 };
  } };
  const result = await enrich(store, api, { categories: [39, 32], limit: 10, maxSeconds: 10, retryFailed: false }, quiet);
  assert.equal(result.completed, 2);
  assert.deepEqual(selected, [{ after: '', category: 39 }, { after: '900', category: 39 },
    { after: '', category: 32 }, { after: '100', category: 32 }]);
});

test('workflow schedules, budgets, dispatch inputs and report artifacts match relay configuration', () => {
  const workflow = parse(read('.github/workflows/daily_tour_batch.yml'));
  assert.deepEqual(workflow.on.schedule.map((r: { cron: string }) => r.cron), ['0 17 * * *', '0 19 * * *', '0 21 * * *']);
  assert.equal(workflow.on.workflow_dispatch.inputs.limit.default, 25000);
  assert.equal(workflow.on.workflow_dispatch.inputs.category.default, 'all');
  assert.ok(workflow.jobs.enrich.timeout_minutes === undefined);
  assert.ok(workflow.jobs.enrich['timeout-minutes'] > 90);
  assert.equal(workflow.jobs.enrich.if, undefined);
  const run = workflow.jobs.enrich.steps.find((step: { run?: string }) => step.run?.includes('batch-tour-enrichment')).run;
  assert.match(run, /categories='12,14,15,28'; ceiling=40000/);
  assert.match(run, /categories='39,32,38,25'; ceiling=80000/);
  assert.match(run, /categories='all'; ceiling=100000/);
  assert.match(run, /--retry-failed/); assert.match(run, /status.*== 2/);
  assert.doesNotMatch(run, /\$\{\{\s*inputs\./);
  const master = parse(read('.github/workflows/daily_places_sync.yml'));
  assert.equal(master.on.schedule[0].cron, '0 16 * * *');
  assert.equal(master.concurrency.group, workflow.concurrency.group);
  assert.match(master.jobs.sync.steps.find((s: { run?: string }) => s.run?.includes('sync:daily-master')).run, /sync:daily-master/);
  assert.doesNotMatch(read('.github/workflows/daily_tour_batch.yml'), /8372|8,372|sync_tour_enrichment_full/);
});

test('CLI category and date selections are validated and report denominator comes from DB', () => {
  const parsed = options(['--categories', '39,32', '--since', '2026-10-27']);
  assert.equal(parsed.help, false);
  if (!parsed.help) assert.deepEqual(parsed.categories, [39, 32]);
  for (const category of ['0', '12,12', '12;drop table places', 'all,12', '']) {
    assert.throws(() => options(['--categories', category]));
  }
  assert.throws(() => options(['--since', '2026-02-30']));
  const snapshot = { database_total: 55555, active_total: 55000, selected_total: 777,
    completed: 500, remaining: 277, failed: 4, pending: 3, unprocessed: 270,
    completion_percent: 64.35, quota: { used: 456, limit: 100000, blocked: false } } as Snapshot;
  const output = summary({ job: 'enrichment', status: 'completed', started_at: '', before: snapshot, after: snapshot });
  assert.match(output, /55555/); assert.match(output, /777/); assert.match(output, /64.35/);
  assert.doesNotMatch(output, /8372|8,372/);
});

test('relay migration: dynamic stats, category filtering, flags, checkpoint and cumulative wave budgets', async () => {
  const db = new PGlite();
  try {
    await db.exec(`create role anon; create role authenticated; create role service_role bypassrls;
      create table public.places(content_id varchar(50) primary key,content_type_id varchar(10),title varchar(255),modified_time varchar(30),is_active boolean default true);
      grant select,insert,update,delete on public.places to service_role;
      grant select on public.places to anon,authenticated;`);
    await db.exec(read('supabase/migrations/20261028074529_batch_tour_enrichment.sql'));
    const migration = read('supabase/migrations/20261028084311_daily_tour_relay.sql');
    await db.exec(migration); await db.exec(migration);
    await db.exec(`insert into public.places(content_id,content_type_id,title,modified_time,is_active)
      values('1','12','A','20261028120000',true),('2','25','Course','20261028120000',true),('3','39','Food','20261028120000',true),('4','32','Inactive','20261028120000',false);
      set role service_role;`);
    const snapshot = async (cats: string[] | null = null) => (await db.query<{ data: Snapshot }>('select public.batch_tour_snapshot($1,$2) as data', ['a'.repeat(64), cats])).rows[0].data;
    assert.equal((await snapshot()).database_total, 4); assert.equal((await snapshot()).active_total, 3);
    assert.equal((await snapshot(['39'])).selected_total, 1);
    const claim = (await db.query<{ content_id: string; claim_token: string }>("select * from public.claim_batch_tour_places_filtered(100,'',false,array['39'])")).rows;
    assert.deepEqual(claim.map(r => r.content_id), ['3']);
    assert.equal((await snapshot()).pending, 1);
    await db.query("select public.complete_batch_tour_place('3',$1,'overview','{}',null,'[]')", [claim[0].claim_token]);
    assert.equal((await snapshot()).completed, 1); assert.equal((await snapshot()).remaining, 2);
    assert.equal((await snapshot()).completion_percent, 33.33);
    assert.equal((await db.query<{ flag: boolean }>("select enrichment_completed as flag from public.places where content_id='3'")).rows[0].flag, true);
    await db.exec("update public.places set modified_time='20261029120000' where content_id='3'");
    assert.equal((await snapshot()).completed, 0);
    assert.equal((await db.query<{ flag: boolean }>("select enrichment_completed as flag from public.places where content_id='3'")).rows[0].flag, false);
    assert.equal((await db.query<{ flag: boolean }>("select enrichment_completed as flag from public.places where content_id='1'")).rows[0].flag, false);
    const key = 'a'.repeat(64);
    const reserve = async (cap: number) => (await db.query<{ ok: boolean }>('select public.reserve_batch_tour_call($1,$2,39999,false) as ok', [key, cap])).rows[0].ok;
    assert.equal(await reserve(40000), true); assert.equal(await reserve(40000), false);
    assert.equal(await reserve(80000), true); assert.equal((await snapshot()).quota.used, 40001);
    await db.query("select public.complete_daily_master((now() at time zone 'Asia/Seoul')::date-3)");
    assert.equal((await db.query<{ ok: boolean }>("select public.daily_master_since((now() at time zone 'Asia/Seoul')::date-1)=(now() at time zone 'Asia/Seoul')::date-3 as ok")).rows[0].ok, true);
    await db.exec('set role anon');
    await assert.rejects(db.query('select public.batch_tour_snapshot()'));
    await assert.rejects(db.query("select public.complete_daily_master(current_date)"));
  } finally { await db.close(); }
});

import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
test('workflow shell selects the scheduled wave and handles quota deferral versus failures', context => {
  const bash = process.platform === 'win32' ? 'C:/Program Files/Git/bin/bash.exe' : '/bin/bash';
  if (!existsSync(bash)) { context.skip('Bash not installed'); return; }
  const workflow = parse(read('.github/workflows/daily_tour_batch.yml'));
  const script = workflow.jobs.enrich.steps.find((step: { run?: string }) => step.run?.includes('batch-tour-enrichment')).run;
  const directory = mkdtempSync(join(tmpdir(), 'fittour-workflow-'));
  try {
    const run = (schedule: string, extra: NodeJS.ProcessEnv = {}) => spawnSync(bash, ['-e', '-o', 'pipefail', '-c',
      `npm() { printf '%s\\n' "$@"; return "\${STUB_EXIT:-0}"; }\n${script}`], {
      cwd: directory, encoding: 'utf8', env: { ...process.env, EVENT_NAME: 'schedule', SCHEDULE: schedule,
        INPUT_WAVE: '3', INPUT_CATEGORY: 'all', INPUT_LIMIT: '25000', INPUT_SECONDS: '5400', INPUT_RETRY: 'false', ...extra },
    });
    for (const [schedule, categories, cap] of [['0 17 * * *', '12,14,15,28', '40000'],
      ['0 19 * * *', '39,32,38,25', '80000'], ['0 21 * * *', 'all', '100000']]) {
      const result = run(schedule);
      assert.equal(result.status, 0, result.stderr);
      assert.ok(result.stdout.includes(`--categories\n${categories}\n`));
      assert.ok(result.stdout.includes(`--daily-limit\n${cap}\n`));
      assert.equal(result.stdout.includes('--retry-failed'), schedule === '0 21 * * *');
    }
    assert.equal(run('0 17 * * *', { STUB_EXIT: '2' }).status, 0);
    assert.equal(run('0 17 * * *', { STUB_EXIT: '1' }).status, 1);
    const manual = run('', { EVENT_NAME: 'workflow_dispatch', INPUT_WAVE: '2', INPUT_CATEGORY: '39', INPUT_LIMIT: '7' });
    assert.equal(manual.status, 0); assert.ok(manual.stdout.includes('--categories\n39\n'));
    assert.ok(manual.stdout.includes('--limit\n7\n'));
  } finally { rmSync(directory, { recursive: true, force: true }); }
});
