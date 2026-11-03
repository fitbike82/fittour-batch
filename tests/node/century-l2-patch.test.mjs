import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { PGlite } from '@electric-sql/pglite';

test('L2 patch uses evidence priority, preserves values and is idempotent', async () => {
  const db = new PGlite();
  try {
    await db.exec(`CREATE TABLE public.places (
      content_id text PRIMARY KEY, title text, is_century_store boolean,
      category_l1 text, category_l2 text, filter_tags text[],
      century_store_info jsonb, updated_at timestamptz DEFAULT '2000-01-01Z');`);
    const cases = [
      ['한우', ['냉면'], null, null, '소고기/구이', '음식점'],
      ['한식', ['냉면'], null, null, '냉면/밀면', '음식점'],
      ['일반음식점', [], null, null, null, null],
      ['식당', ['한식'], null, null, null, null],
      ['갈비', [], null, null, null, null],
      ['생선', [], null, null, null, null],
      ['생선구이', [], null, null, '게장/생선구이', '음식점'],
      ['삼계탕', [], null, null, '닭/오리/백숙', '음식점'],
      ['추어탕', [], null, null, '보양/추어/어탕', '음식점'],
      ['장어탕', [], null, null, '보양/추어/어탕', '음식점'],
      ['닭갈비', [], null, null, '닭/오리/백숙', '음식점'],
      ['떡볶이', [], null, null, '만두/분식', '음식점'],
      ['떡갈비', [], null, null, null, null],
      ['떡국', [], null, null, null, null],
      ['떡만두', [], null, null, '만두/분식', '음식점'],
      ['떡', [], '음식점', null, '떡/전통명과', '카페/디저트'],
      ['베이커리', [], null, null, '베이커리/빵', '카페/디저트'],
      ['카페', [], '기존분류', null, '카페/음료', '카페/디저트'],
      ['한우', [], '수동분류', null, '소고기/구이', '수동분류'],
      ['한우', [], '음식점', '냉면/밀면', '냉면/밀면', '음식점'],
      ['한우 냉면', ['카페'], null, null, null, null],
      [null, ['한우', '냉면'], null, null, null, null],
      [null, ['냉', '면'], null, null, null, null],
      ['회관', [], null, null, null, null],
      ['회', [], null, null, '활어회/해물', '음식점'],
    ];
    for (const [i, [business,tags,l1,l2]] of cases.entries()) {
      await db.query(`INSERT INTO places(content_id,title,is_century_store,category_l1,category_l2,filter_tags,century_store_info)
        VALUES($1,$1,true,$2,$3,$4,$5::jsonb)`, [String(i),l1,l2,tags,JSON.stringify({business})]);
    }
    await db.exec(`INSERT INTO places(content_id,is_century_store,century_store_info)
      VALUES('noncentury',false,'{"business":"한우"}');`);
    const sql = readFileSync(new URL('../../sql/patch_century_store_category_l2.sql', import.meta.url),'utf8');
    await db.exec(sql);
    const first = (await db.query('SELECT * FROM places ORDER BY content_id')).rows;
    for (const [i, c] of cases.entries()) {
      const row = first.find(r => r.content_id === String(i));
      assert.equal(row.category_l2,c[4], `case ${i}: ${c[0]}`);
      assert.equal(row.category_l1,c[5], `case ${i} L1`);
      if (c[4] === null || c[3] !== null) assert.equal(new Date(row.updated_at).getUTCFullYear(),2000);
    }
    assert.equal(first.find(r=>r.content_id==='noncentury').category_l2,null);
    const second = await db.exec(sql);
    assert.equal(second.flatMap(r=>r.rows).find(r=>'actually_updated' in r).actually_updated,0);
    assert.deepEqual((await db.query('SELECT * FROM places ORDER BY content_id')).rows,first);
  } finally { await db.close(); }
});
