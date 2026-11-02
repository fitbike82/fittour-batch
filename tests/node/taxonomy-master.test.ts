import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { CATEGORY_L1_MAP, CATEGORY_L2_MASTER, FOOD_L2_RULES, extractFilterTags,
  resolveFoodCategory, exportTaxonomy } from '../../src/lib/taxonomy.js';

test('PM master and generated Python bridge stay identical', () => {
  const bridge = JSON.parse(readFileSync(new URL('../../scripts/place_taxonomy.json', import.meta.url), 'utf8'));
  assert.deepEqual(bridge, exportTaxonomy(), 'Run npm run taxonomy:sync');
  assert.equal(Object.keys(CATEGORY_L1_MAP).length,8);
  assert.equal(CATEGORY_L1_MAP['15'],'축제/행사');
  assert.equal(CATEGORY_L2_MASTER.FOOD.length,20);
  for (const rule of FOOD_L2_RULES) assert.ok((CATEGORY_L2_MASTER.FOOD as readonly string[]).includes(rule.l2));
});

test('ordered food rules preserve dessert exceptions and avoid rice-cake false positives', () => {
  assert.deepEqual(resolveFoodCategory('돈가스와 냉면'),{l1:'음식점',l2:'냉면/밀면'});
  assert.deepEqual(resolveFoodCategory('제과 제빵'),{l1:'카페/디저트',l2:'베이커리/빵'});
  assert.deepEqual(resolveFoodCategory('떡볶이 김밥'),{l1:'음식점',l2:'만두/분식'});
  for (const text of ['떡갈비','떡국','떡만두국']) assert.equal(resolveFoodCategory(text).l1,'음식점');
  assert.deepEqual(resolveFoodCategory('찹쌀떡'),{l1:'카페/디저트',l2:'떡/전통명과'});
  assert.deepEqual(resolveFoodCategory('알수없는상호'),{l1:'음식점',l2:null});
});

test('filter tags normalize aliases, parentheses, suffixes, duplicates and reserved tags', () => {
  assert.deepEqual(extractFilterTags('돈가스/돈까스, 까페·카페 (전문점) 등 갈비전문 백년가게 떡'),
    ['돈까스','카페','갈비','떡']);
  assert.deepEqual(extractFilterTags(''),[]);
});
