import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createSearchAdapters, SearchError, searchPlace } from '../../src/lib/search.js';

const env = { NAVER_CLIENT_ID: 'id', NAVER_CLIENT_SECRET: 'secret', KAKAO_REST_API_KEY: 'key' };
test('environment enables Naver only with both keys; Kakao alias remains supported', () => {
  assert.deepEqual(createSearchAdapters(env).map(a => a.name), ['naver', 'kakao']);
  assert.deepEqual(createSearchAdapters({ NAVER_CLIENT_ID: 'id', KAKAO_API_KEY: 'key' }).map(a => a.name), ['kakao']);
  assert.deepEqual(createSearchAdapters({ NAVER_CLIENT_ID: ' ', NAVER_CLIENT_SECRET: 'secret' }), []);
});

test('exhausts regional/title queries on Naver before Kakao and normalizes responses', async () => {
  const calls: Array<{ url: URL; headers: Headers }> = [];
  const adapters = createSearchAdapters(env, async (input, init) => {
    const url = new URL(String(input));
    calls.push({ url, headers: new Headers(init?.headers) });
    return Response.json(url.hostname === 'openapi.naver.com' ? { items: [] }
      : { documents: [{ title: '맛집', url: 'https://example.com/post', contents: '후기', datetime: '2026' }] });
  });
  const result = await searchPlace({ title: '(주) 맛집 본점', sigungu: '강남구' }, adapters);
  assert.equal(result.provider, 'kakao');
  assert.deepEqual(calls.map(c => c.url.searchParams.get('query')), ['강남 맛집', '맛집', '강남 맛집']);
  assert.equal(calls[0].headers.get('X-Naver-Client-Secret'), 'secret');
  assert.equal(calls[2].headers.get('Authorization'), 'KakaoAK key');
  assert.equal(calls[0].url.searchParams.get('sort'), 'sim');
  assert.equal(calls[2].url.searchParams.get('sort'), 'accuracy');
  assert.equal(result.documents[0].contents, '후기');
});

test('Naver success short circuits and maps link, description and postdate', async () => {
  let calls = 0;
  const result = await searchPlace({ title: '맛집' }, createSearchAdapters(env, async () => {
    calls++;
    return Response.json({ items: [{ title: '<b>맛집</b>', link: 'https://example.com', description: '후기', postdate: '20261029' }] });
  }));
  assert.equal(calls, 1);
  assert.equal(result.provider, 'naver');
  assert.equal(result.documents[0].datetime, '20261029');
});

test('provider errors fallback without leaking raw errors; all failures are distinct from zero results', async () => {
  for (const failed of [() => new Response('', { status: 429 }), () => new Response('broken'),
    () => Response.json({ items: null }), () => { throw new Error('secret'); }]) {
    let calls = 0;
    const result = await searchPlace({ title: '맛집', sigungu: '강남구' }, createSearchAdapters(env, async () => {
      return ++calls === 1 ? failed() : Response.json({ documents: [] });
    }));
    assert.equal(calls, 3);
    assert.equal(result.documents.length, 0);
    assert.ok(!JSON.stringify(result).includes('secret'));
  }
  await assert.rejects(searchPlace({ title: '맛집' }, createSearchAdapters(env, async () => new Response('', { status: 503 }))),
    /all_search_providers_failed/);
  await assert.rejects(searchPlace({ title: '맛집' }, []), SearchError);
  assert.deepEqual((await searchPlace({ title: '(주)' }, [])).attempts, []);
});
