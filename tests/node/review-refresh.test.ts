import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { PGlite } from '@electric-sql/pglite';
const read = (path: string) => readFileSync(new URL(`../../${path}`, import.meta.url), 'utf8');

test('review refresh covers empty points, initial work barrier, history and preservation on failure', async () => {
  const db = new PGlite();
  try {
    await db.exec(`create role service_role bypassrls; create role anon; create role authenticated;
      create table public.places(content_id varchar primary key,content_type_id varchar,title text,address text,
        modified_time text,is_active boolean default true,needs_ai_summary boolean default true);
      create table public.place_ai_summaries(content_id varchar primary key,quality_status text,honest_summary text,
        pros jsonb,cons jsonb,visit_tips text,model text,source_reviews jsonb,updated_at timestamptz);
      grant select,insert,update on public.places,public.place_ai_summaries to service_role;
      insert into public.places(content_id,title,modified_time) values('a','Park','v1'),('b','Museum','v1');`);
    await db.exec(read('supabase/migrations/20261020094517_ai_review_two_pass_queue.sql'));
    await db.exec(read('sql/ai_review_refresh.sql'));
    await db.exec(read('sql/ai_review_refresh.sql'));
    await db.exec('set role service_role');
    const due = async () => (await db.query<{ n: number }>('select public.enqueue_due_ai_reviews(50,180) n')).rows[0].n;
    const claim = async () => (await db.query<{ content_id: string; claim_token: string }>('select * from public.claim_ai_review_work(50)')).rows;
    const finish = async (row: { content_id: string; claim_token: string }, status: string, label = 'summary', basis = 'review_analysis') => {
      const data = { summary_lines: [label,'line2','line3'],pros:[],cons:[],visit_tips:'tip',analysis_basis:basis };
      return (await db.query<{ ok: boolean }>(`select public.complete_ai_review_work($1,$2,'v1',$3,$4::jsonb) ok`,
        [row.content_id,row.claim_token,status,JSON.stringify(data)])).rows[0].ok;
    };
    assert.equal(await due(), 0); // First-pass queue has precedence.
    for (const row of await claim()) assert.equal(await finish(row,'ready'), true);
    assert.equal(await due(), 0);
    await db.exec("update public.place_ai_summaries set updated_at=now()-interval '200 days' where content_id='a'; update public.ai_review_jobs set attempted_at=now()-interval '200 days' where content_id='a'");
    assert.equal(await due(), 1);
    let row = (await claim())[0];
    assert.equal(row.content_id, 'a');
    assert.equal(await finish(row,'insufficient'), true);
    let saved = (await db.query<{ honest_summary: string; last_checked_at: string }>("select honest_summary,last_checked_at from public.place_ai_summaries where content_id='a'")).rows[0];
    assert.match(saved.honest_summary, /^summary/);
    assert.ok(saved.last_checked_at);
    row = (await claim())[0];
    assert.equal(await finish(row,'ready','basic','basic_info'), true);
    saved = (await db.query<{ honest_summary: string; last_checked_at: string }>("select honest_summary,last_checked_at from public.place_ai_summaries where content_id='a'")).rows[0];
    assert.match(saved.honest_summary,/^summary/); // No downgrade to basic info.
    await db.exec("update public.ai_review_jobs set attempted_at=now()-interval '200 days' where content_id='a'");
    assert.equal(await due(),1);
    row=(await claim())[0];
    assert.equal(await finish(row,'ready','new analysis'),true);
    const history = (await db.query<{ n: number }>("select count(*)::int n from public.ai_review_summary_history where content_id='a'")).rows[0].n;
    assert.equal(history,1);
    await db.exec("update public.places set modified_time='v2' where content_id='a'");
    assert.equal((await claim())[0].content_id,'a');
    assert.equal(await finish(row,'ready','stale worker'),false);
    await db.exec('set role anon');
    await assert.rejects(db.query('select public.enqueue_due_ai_reviews(50,180)'));
    await assert.rejects(db.query('select * from public.ai_review_summary_history'));
  } finally { await db.close(); }
});
