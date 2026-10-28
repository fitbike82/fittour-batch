import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { PGlite } from '@electric-sql/pglite';

const migration = readFileSync(new URL('../../supabase/migrations/20261028074529_batch_tour_enrichment.sql', import.meta.url), 'utf8').replace(/^\uFEFF/, '');
test('Postgres migration: claims, atomic course writes, retry, source invalidation, quota and RLS', async () => {
  const db = new PGlite();
  try {
    await db.exec(`create role anon; create role authenticated; create role service_role bypassrls;
      create table public.places(content_id varchar(50) primary key,content_type_id varchar(10),
      title varchar(255),modified_time varchar(30),is_active boolean default true);
      grant select,insert,update,delete on public.places to service_role;
      grant select on public.places to anon,authenticated;
      insert into public.places values('1','25','Course','20261028120000',true),('2','12','Place','20261028120000',true);`);
    await db.exec(migration);
    await db.exec(migration); // Additive migration remains safe to reapply.
    await db.exec('set role service_role;');
    const claim = await db.query<{ content_id: string; claim_token: string }>("select * from public.claim_batch_tour_places(1,'',false)");
    const token = claim.rows[0].claim_token;
    assert.equal(claim.rows[0].content_id, '1');
    const concurrent = await db.query<{ content_id: string }>("select * from public.claim_batch_tour_places(1,'',false)");
    assert.equal(concurrent.rows[0].content_id, '2');
    const finish = (stops: unknown, t = token) => db.query<{ ok: boolean }>(
      'select public.complete_batch_tour_place($1,$2,$3,$4::jsonb,$5::jsonb,$6::jsonb) as ok',
      ['1', t, 'overview', JSON.stringify({ distance: '3km' }), JSON.stringify({ title: 'Course', distance: '3km' }), JSON.stringify(stops)]);
    assert.equal((await finish([{ subnum: 1, subname: 'First' }, { subnum: 2, subname: 'Second' }])).rows[0].ok, true);
    assert.equal((await finish([{ subnum: 1, subname: 'Stale' }])).rows[0].ok, false);
    await db.exec("update public.places set modified_time='20261029120000' where content_id='1';");
    const changed = await db.query<{ enrich_status: string | null }>("select enrich_status from public.places where content_id='1'");
    assert.equal(changed.rows[0].enrich_status, null);
    const again = await db.query<{ claim_token: string }>("select * from public.claim_batch_tour_places(1,'',false)");
    const nextToken = again.rows[0].claim_token;
    await assert.rejects(finish([{ subnum: 1, subname: 'New' }, { subnum: 1, subname: 'Duplicate' }], nextToken));
    const unchanged = await db.query<{ subname: string }>("select subname from public.tour_course_places order by subnum");
    assert.deepEqual(unchanged.rows.map(r => r.subname), ['First', 'Second']);
    await db.query('select public.release_batch_tour_place($1,$2,true)', ['1', nextToken]);
    assert.equal((await db.query("select * from public.claim_batch_tour_places(1,'',false)")).rows.length, 0);
    assert.equal((await db.query("select * from public.claim_batch_tour_places(1,'',true)")).rows.length, 1);
    await db.exec("update public.places set enrich_claim_until=now()-interval '1 minute' where content_id='2';");
    const recovered = await db.query<{ content_id: string }>("select * from public.claim_batch_tour_places(1,'',false)");
    assert.equal(recovered.rows[0].content_id, '2');
    const key = 'a'.repeat(64);
    const reserve = () => db.query<{ ok: boolean }>('select public.reserve_batch_tour_call($1,2,0,false) as ok', [key]);
    assert.equal((await reserve()).rows[0].ok, true); assert.equal((await reserve()).rows[0].ok, true);
    assert.equal((await reserve()).rows[0].ok, false);
    assert.equal((await db.query<{ ok: boolean }>('select public.reserve_batch_tour_call($1,100000,0,true) as ok', ['b'.repeat(64)])).rows[0].ok, false);
    assert.equal((await db.query<{ ok: boolean }>('select public.reserve_batch_tour_call($1,100000,0,false) as ok', ['b'.repeat(64)])).rows[0].ok, false);
    await db.exec("update public.places set is_active=false where content_id='1'; set role anon;");
    assert.equal((await db.query('select * from public.tour_courses')).rows.length, 0);
    assert.equal((await db.query('select * from public.tour_course_places')).rows.length, 0);
    await assert.rejects(db.query("select * from public.claim_batch_tour_places(1,'',false)"));
  } finally { await db.close(); }
});
