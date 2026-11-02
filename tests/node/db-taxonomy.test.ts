import assert from 'node:assert/strict';
import { test } from 'node:test';
import { taxonomyReport, type TaxonomyRow } from '../../scripts/analyze-db-taxonomy.js';

test('taxonomy includes inactive and uncoded rows and counts keyword presence per place', () => {
  const row = (id: string, type: string, title: string, active: boolean): TaxonomyRow => ({
    content_id: id, content_type_id: type, title, cat1: 'A05', cat2: 'A0502', cat3: null, is_active: active,
  });
  const report = taxonomyReport([
    row('1', '39', '갈비 갈비 카페', true), row('2', '39', '돈까스', false),
    row('3', '25', '코스|샘플\n이름', true),
  ]);
  assert.ok(report.includes('| 39 | 음식점 | 2 | 1 | 1 | 0 |'));
  assert.ok(report.includes('| (미등록) | 3 |'));
  assert.match(report, /\| \d+ \| 갈비 \| 1 \|/);
  assert.match(report, /\| \d+ \| 돈가스 \| 1 \|/);
  assert.ok(report.includes('코스\\|샘플 이름'));
  assert.ok(report.includes('여행코스(25) cat2 분포 (고유 등록 코드 1개)'));
});

test('empty taxonomy still shows all eight source types without fabricated keywords', () => {
  const report = taxonomyReport([]);
  assert.ok(report.includes('| 28 | 레포츠 | 0 | 0 | 0 | 0 |'));
  assert.ok(!/\| \d+ \| 갈비 \|/.test(report));
});
