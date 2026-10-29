import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildSmartQueries, sanitizePlaceTitle } from '../../src/utils/querySanitizer.js';

test('cleans corporate, branch, bracket and punctuation noise without joining words', () => {
  assert.equal(sanitizePlaceTitle('(주) 맛집 [본점] (제2주차장) 직영점 2호점 ★'), '맛집');
  assert.equal(sanitizePlaceTitle('㈜ 주식회사 A&B [안내(임시)] 본점'), 'A B');
  assert.equal(sanitizePlaceTitle('Cafe 123 한글\n  카페'), 'Cafe 123 한글 카페');
});

test('queries prefer sigungu, shorten long administrative suffixes and deduplicate', () => {
  assert.deepEqual(buildSmartQueries({ title: '(주) 맛집 [본점]', sido: '서울특별시', sigungu: '강남구' }),
    ['강남 맛집', '맛집']);
  assert.deepEqual(buildSmartQueries({ title: '강남 맛집', sigungu: '강남구' }), ['강남 맛집']);
  assert.deepEqual(buildSmartQueries({ title: '맛집', sido: '제주특별자치도', sigungu: '  ' }), ['제주 맛집', '맛집']);
  assert.deepEqual(buildSmartQueries({ title: '맛집', sido: '서울특별시' }), ['서울 맛집', '맛집']);
  assert.deepEqual(buildSmartQueries({ title: '맛집' }), ['맛집']);
  assert.deepEqual(buildSmartQueries({ title: '(주) [본점] ★', sigungu: '강남구' }), []);
});
