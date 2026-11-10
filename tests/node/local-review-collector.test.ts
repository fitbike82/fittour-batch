import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import {
  buildSearchQueries,
  cleanSearchText,
  collectPlaceSources,
  createNaverSearch,
  hasNonEmptyCollectionFile,
  loadSearchCredentials,
  mergeUniqueSources,
  readPlacesCsv,
  saveCollectionFile,
  toCollectionFile,
  type Place,
  type RawSource,
} from '../../src/lib/local-review-collector.js';

const place: Place = {
  content_id: '4013196', title: 'CU 올림픽광장점', sido: '서울특별시', sigungu: '송파구',
  addr1: '서울특별시 송파구 올림픽로 424 (방이동)', pv_30d: 22,
};
const source = (provider: 'naver' | 'kakao', url: string): RawSource => ({
  provider, title: `${provider} title`, url, mobile_url: url, snippet: `${provider} snippet`,
});

test('builds primary and broader fallback queries', () => {
  assert.deepEqual(buildSearchQueries(place), {
    primary: '서울특별시 송파구 CU 올림픽광장점', fallback: '송파구 CU 올림픽광장점 후기',
  });
});

test('strips tags, entities and repeated whitespace from search text', () => {
  assert.equal(cleanSearchText('<b>서울</b> &quot;여행&quot;  후기&#33;'), '서울 "여행" 후기!');
});

test('prefers KAKAO_REST_API_KEY and makes incomplete Naver credentials optional', () => {
  assert.deepEqual(loadSearchCredentials({
    KAKAO_REST_API_KEY: 'rest-key', KAKAO_API_KEY: 'legacy-key', NAVER_CLIENT_ID: 'id-only',
  }), { kakaoApiKey: 'rest-key' });
});

test('silently disables Naver after an authorization failure', async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => { calls++; return new Response('', { status: 401 }); };
  try {
    const search = createNaverSearch('invalid-id', 'invalid-secret');
    assert.deepEqual(await search('첫 검색'), []);
    assert.deepEqual(await search('두 번째 검색'), []);
    assert.equal(calls, 1);
  } finally { globalThis.fetch = originalFetch; }
});

test('calls NAVER API HUB with its required endpoint and credential headers', async () => {
  const originalFetch = globalThis.fetch;
  let requestUrl: URL | undefined;
  let requestHeaders: Headers | undefined;
  globalThis.fetch = async (input, init) => {
    requestUrl = new URL(String(input));
    requestHeaders = new Headers(init?.headers);
    return Response.json({ items: [{
      title: '<b>제목</b>', link: 'https://blog.naver.com/example/1', description: '설명',
    }] });
  };
  try {
    const results = await createNaverSearch('hub-id', 'hub-secret')('서울 관광지');
    assert.ok(requestUrl);
    assert.ok(requestHeaders);
    assert.equal(requestUrl.origin + requestUrl.pathname,
      'https://naverapihub.apigw.ntruss.com/search/v1/blog');
    assert.equal(requestUrl.searchParams.get('display'), '5');
    assert.equal(requestUrl.searchParams.get('format'), 'json');
    assert.equal(requestHeaders.get('X-NCP-APIGW-API-KEY-ID'), 'hub-id');
    assert.equal(requestHeaders.get('X-NCP-APIGW-API-KEY'), 'hub-secret');
    assert.equal(requestHeaders.has('X-Naver-Client-Id'), false);
    assert.equal(results[0].mobile_url, 'https://m.blog.naver.com/example/1');
  } finally { globalThis.fetch = originalFetch; }
});

test('deduplicates sources by URL while ignoring fragments', () => {
  assert.deepEqual(mergeUniqueSources(
    [source('naver', 'https://example.com/post#one')],
    [source('kakao', 'https://example.com/post#two')],
  ), [source('naver', 'https://example.com/post')]);
});

test('runs both fallback searches only when primary unique result count is below four', async () => {
  const calls: string[] = [];
  const naver = async (query: string) => {
    calls.push(`naver:${query}`);
    return query.endsWith('후기') ? [source('naver', 'https://n.example/2')] : [source('naver', 'https://n.example/1')];
  };
  const kakao = async (query: string) => {
    calls.push(`kakao:${query}`);
    return query.endsWith('후기') ? [source('kakao', 'https://k.example/2')] : [source('kakao', 'https://k.example/1')];
  };
  const results = await collectPlaceSources(place, naver, kakao);
  assert.equal(results.length, 4);
  assert.deepEqual(calls, [
    'naver:서울특별시 송파구 CU 올림픽광장점', 'kakao:서울특별시 송파구 CU 올림픽광장점',
    'naver:송파구 CU 올림픽광장점 후기', 'kakao:송파구 CU 올림픽광장점 후기',
  ]);
});

test('reads the project CSV in source order with numeric pv_30d', async () => {
  const rows = await readPlacesCsv(path.resolve('docs/관광지_list_1차.csv'));
  assert.deepEqual(rows[0], place);
  assert.equal(rows[1].content_id, '2930839');
});

test('parses quoted commas and treats null cells as empty strings', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'review-csv-'));
  try {
    const csvPath = path.join(directory, 'targets.csv');
    await writeFile(csvPath, 'content_id,title,sido,sigungu,addr1,pv_30d\n1,"장소, 이름",서울,null,"주소, 상세",null\n');
    assert.deepEqual(await readPlacesCsv(csvPath), [{
      content_id: '1', title: '장소, 이름', sido: '서울', sigungu: '', addr1: '주소, 상세', pv_30d: null,
    }]);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('writes complete JSON and detects non-empty files for resumability', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'review-output-'));
  try {
    assert.equal(await hasNonEmptyCollectionFile(directory, place.content_id), false);
    await saveCollectionFile(directory, toCollectionFile(place, [source('naver', 'https://blog.naver.com/test/1')], new Date(0)));
    assert.equal(await hasNonEmptyCollectionFile(directory, place.content_id), true);
  } finally { await rm(directory, { recursive: true, force: true }); }
});
