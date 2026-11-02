import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { PGlite } from '@electric-sql/pglite';

test('taxonomy setup is idempotent, indexed, revision guarded and preserves curated classification', async () => {
  const db = new PGlite();
  try {
    await db.exec(`create role anon; create role authenticated; create role service_role bypassrls;
      create table public.places(content_id text primary key,content_type_id text,is_active boolean,modified_time text);
      grant select,insert,update on public.places to service_role;`);
    const sql = readFileSync(new URL('../../sql/place_taxonomy_setup.sql', import.meta.url), 'utf8');
    await db.exec(sql); await db.exec(sql);
    await db.exec(`insert into public.places(content_id,content_type_id,is_active,modified_time,is_century_store)
      values('1','39',true,'v1',true); set role service_role`);
    const apply = async (revision: string, taxonomy: object) => (await db.query<{ok:boolean}>(
      'select public.apply_ai_place_taxonomy($1,$2,$3::jsonb) ok', ['1',revision,JSON.stringify(taxonomy)])).rows[0].ok;
    const taxonomy = { category_l1: '음식점', category_l2: '냉면/밀면', filter_tags: ['갈비','냉면','갈비'] };
    assert.equal(await apply('stale',taxonomy),false);
    assert.equal(await apply('v1',taxonomy),true);
    let saved = (await db.query<{filter_tags:string[];category_l2:string}>('select filter_tags,category_l2 from public.places')).rows[0];
    assert.deepEqual(saved.filter_tags,['갈비','냉면','백년가게']);
    assert.equal(await apply('v1',{category_l1:'카페/디저트',category_l2:'베이커리/빵',filter_tags:['베이커리']}),true);
    await assert.rejects(apply('v1',{category_l1:'카페/디저트',category_l2:null,filter_tags:[]}));
    assert.equal(await apply('v1',taxonomy),true);
    await db.exec("update public.places set taxonomy_source='century_csv:20220713:v1'");
    assert.equal(await apply('v1',{...taxonomy, category_l2:null, filter_tags:[]}),false);
    saved = (await db.query<{filter_tags:string[];category_l2:string}>('select filter_tags,category_l2 from public.places')).rows[0];
    assert.equal(saved.category_l2,'냉면/밀면');
    await assert.rejects(apply('v1',{...taxonomy,category_l1:'숙박'}));
    await assert.rejects(apply('v1',{...taxonomy,filter_tags:['백년가게']}));
    const indexes = (await db.query<{indexname:string}>("select indexname from pg_indexes where tablename='places'")).rows;
    assert.ok(indexes.some(row=>row.indexname==='idx_places_filter_tags'));
    await db.exec('set role anon');
    await assert.rejects(apply('v1',taxonomy));
  } finally { await db.close(); }
});
