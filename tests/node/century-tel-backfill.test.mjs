import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { PGlite } from '@electric-sql/pglite';

test('century tel backfill preserves existing data, filters source and is idempotent', async () => {
  const db = new PGlite();
  try {
    await db.exec(`CREATE TABLE public.places (
      content_id text PRIMARY KEY, title text, tel text,
      is_century_store boolean, century_store_info jsonb,
      updated_at timestamptz DEFAULT '2000-01-01Z');`);
    const fixtures = [
      ['1', null, true, { phone: ' 02-123-4567 ' }],
      ['2', '   ', true, { phone: '+82 (2) 123-4567' }],
      ['3', '02-999-9999', true, { phone: '02-111-1111' }],
      ['4', null, false, { phone: '02-111-1111' }],
      ['5', null, true, null],
      ['6', '', true, {}],
      ['7', null, true, { phone: null }],
      ['8', '', true, { phone: '  ' }],
      ['9', null, true, { phone: ' +---() ' }],
      ['10', null, true, { phone: '\t\n' }],
      ['11', null, true, { phone: { value: '021234567' } }],
      ['12', null, true, { phone: 21234567 }],
    ];
    for (const [id, tel, century, info] of fixtures) {
      await db.query('INSERT INTO public.places(content_id,title,tel,is_century_store,century_store_info) VALUES($1,$1,$2,$3,$4::jsonb)',
        [id, tel, century, JSON.stringify(info)]);
    }
    const sql = readFileSync(new URL('../../sql/task-db-01-century-tel-backfill.sql', import.meta.url), 'utf8');
    const first = await db.exec(sql);
    const rows = first.flatMap(r => r.rows);
    const report = rows.find(r => 'actually_updated' in r);
    assert.deepEqual(report, {
      century_store_total: 11, missing_tel_before: 10, eligible_before: 2,
      actually_updated: 2, missing_tel_after: 8, missing_tel_resolved: 2,
      source_mismatch_count: 0,
    });
    const beforeRerun = (await db.query('SELECT * FROM places ORDER BY content_id')).rows;
    assert.equal(beforeRerun.find(r => r.content_id === '1')?.tel, '02-123-4567');
    for (const row of beforeRerun.filter(r => !['1', '2'].includes(String(r.content_id)))) {
      assert.equal(new Date(String(row.updated_at)).getUTCFullYear(), 2000);
    }
    const second = await db.exec(sql);
    assert.equal(second.flatMap(r => r.rows).find(r => 'actually_updated' in r)?.actually_updated, 0);
    assert.deepEqual((await db.query('SELECT * FROM places ORDER BY content_id')).rows, beforeRerun);
  } finally {
    await db.close();
  }
});
