import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  MAX_FULL_TEXT_LENGTH,
  extractArticleText,
  hasValidFullText,
  normalizeScrapeUrl,
  scrapeSource,
} from '../../src/lib/local-review-scraper.js';

test('normalizes Naver path and legacy query URLs to mobile form', () => {
  assert.equal(normalizeScrapeUrl({ url: 'https://blog.naver.com/writer/123#section' }),
    'https://m.blog.naver.com/writer/123');
  assert.equal(normalizeScrapeUrl({ url: 'https://blog.naver.com/PostView.naver?blogId=writer&logNo=456' }),
    'https://m.blog.naver.com/writer/456');
});

test('extracts Naver SmartEditor body and removes navigation and scripts', () => {
  const repeated = '실제 방문 후기와 주차 공간에 대한 자세한 설명입니다. '.repeat(8);
  const html = `<nav>메뉴</nav><div class="se-main-container"><p>${repeated}</p><script>secret()</script></div>`;
  const text = extractArticleText(html, 'https://m.blog.naver.com/writer/1');
  assert.match(text, /실제 방문 후기/);
  assert.doesNotMatch(text, /메뉴|secret/);
});

test('extracts general article and strictly truncates to 50000 characters', () => {
  const text = extractArticleText(`<article><p>${'가'.repeat(55_000)}</p></article>`, 'https://sample.tistory.com/1');
  assert.equal(text.length, MAX_FULL_TEXT_LENGTH);
});

test('falls back to paragraph blocks when no main container exists', () => {
  const text = extractArticleText(`<html><body><p>${'본문 문장입니다. '.repeat(15)}</p></body></html>`, 'https://example.com/post');
  assert.ok(text.length > 100);
});

test('uses cleaned snippet and failed status after a request failure', async () => {
  const fetcher = async () => new Response('', { status: 404 });
  const result = await scrapeSource({
    url: 'https://example.com/post', snippet: '<b>요약</b> 내용입니다. '.repeat(12),
  }, fetcher as typeof fetch);
  assert.equal(result.scrape_status, 'failed');
  assert.doesNotMatch(result.full_text ?? '', /<b>/);
  assert.equal(result.char_count, result.full_text?.length);
});

test('skips network for an existing body of at least 1000 characters', async () => {
  let calls = 0;
  const source = { url: 'https://example.com/post', full_text: '가'.repeat(1_000), char_count: 1_000,
    scrape_status: 'scraped' as const };
  const result = await scrapeSource(source, (async () => { calls++; throw new Error('unexpected'); }) as typeof fetch);
  assert.equal(result, source);
  assert.equal(calls, 0);
  assert.equal(hasValidFullText(result), true);
});

test('re-scrapes legacy short bodies but skips short bodies completed at the 50000 scale', async () => {
  const legacy = { full_text: '가'.repeat(999), char_count: 999, scrape_status: 'scraped' as const };
  assert.equal(hasValidFullText(legacy), false);
  assert.equal(hasValidFullText({ ...legacy, scrape_max_chars: MAX_FULL_TEXT_LENGTH }), true);
});
