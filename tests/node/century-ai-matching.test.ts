import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { parse } from 'yaml';
import { isFoodBusiness, NON_FOOD_KEYWORDS, FOOD_OVERRIDE_KEYWORDS } from '../../src/lib/century-food-filter.js';

test('PM food filter matches the shared Python data and preserves whitelist precedence', () => {
  const bridge=JSON.parse(readFileSync('scripts/place_taxonomy.json','utf8'));
  assert.deepEqual(bridge.non_food_keywords,NON_FOOD_KEYWORDS);
  assert.deepEqual(bridge.food_override_keywords,FOOD_OVERRIDE_KEYWORDS);
  for(const business of ['미용','안경','방앗간','쌀국수','']) assert.equal(isFoodBusiness(business),false);
  for(const business of ['떡/방앗간','제과','카페 및 서점','갈비','세탁']) assert.equal(isFoodBusiness(business),true);
});

test('AI workflow schedules five KST batches with durable checkpoints and shared Gemini concurrency', () => {
  const workflow=parse(readFileSync('.github/workflows/century_ai_matching.yml','utf8'));
  assert.deepEqual(workflow.on.schedule.map((row:{cron:string})=>row.cron),[
    '0 17 * * *','0 19 * * *','30 19 * * *','30 20 * * *','30 21 * * *',
  ]);
  assert.equal(workflow.concurrency.group,'ai-summary-batch');
  const steps=workflow.jobs.match.steps;
  assert.ok(steps.some((step:{uses?:string})=>step.uses==='actions/download-artifact@v4'));
  const run=steps.find((step:{id:string})=>step.id==='batch').run;
  assert.match(run,/--limit 50/);
  assert.ok(steps.find((step:{id:string})=>step.id==='persist').if.includes('always()'));
});
