import { buildSmartQueries, type SmartQueryParams } from '../utils/querySanitizer.js';

export type SearchDocument = { title: string; url: string; contents: string; datetime: string };
export interface SearchAdapter {
  readonly name: string;
  search(query: string): Promise<SearchDocument[]>;
}
export class SearchError extends Error {
  constructor(public readonly code: string) { super(code); this.name = 'SearchError'; }
}

type Provider = 'naver' | 'kakao';
function adapter(provider: Provider, headers: Record<string, string>, fetcher: typeof fetch): SearchAdapter {
  return {
    name: provider,
    async search(query) {
      const url = new URL(provider === 'naver' ? 'https://naverapihub.apigw.ntruss.com/search/v1/blog'
        : 'https://dapi.kakao.com/v2/search/blog');
      url.searchParams.set('query', query);
      url.searchParams.set(provider === 'naver' ? 'display' : 'size', provider === 'naver' ? '5' : '10');
      url.searchParams.set('sort', provider === 'naver' ? 'sim' : 'accuracy');
      if (provider === 'naver') url.searchParams.set('format', 'json');
      let response: Response;
      try { response = await fetcher(url, { headers, signal: AbortSignal.timeout(15_000) }); }
      catch { throw new SearchError('transport_error'); }
      if (!response.ok) throw new SearchError(`http_${response.status}`);
      let payload: unknown;
      try { payload = await response.json(); } catch { throw new SearchError('invalid_json'); }
      const field = provider === 'naver' ? 'items' : 'documents';
      const rows = payload && typeof payload === 'object' ? (payload as Record<string, unknown>)[field] : undefined;
      if (!Array.isArray(rows)) throw new SearchError('invalid_response');
      return rows.map(row => {
        if (!row || typeof row !== 'object' || Array.isArray(row)) throw new SearchError('invalid_document');
        const { title, url, contents, datetime } = provider === 'naver'
          ? { title: row.title, url: row.link, contents: row.description, datetime: row.postdate }
          : row;
        if (typeof title !== 'string' || typeof url !== 'string' || typeof contents !== 'string') {
          throw new SearchError('invalid_document');
        }
        let parsed: URL;
        try { parsed = new URL(url); } catch { throw new SearchError('invalid_document_url'); }
        if (!['http:', 'https:'].includes(parsed.protocol)) throw new SearchError('invalid_document_url');
        return { title: provider === 'naver' ? title.replace(/<[^>]*>?/g, '') : title,
          url, contents: provider === 'naver' ? contents.replace(/<[^>]*>?/g, '') : contents,
          datetime: typeof datetime === 'string' ? datetime : '' };
      });
    },
  };
}

/** Both Naver credentials must be present; otherwise use Kakao alone. */
export function createSearchAdapters(env: NodeJS.ProcessEnv = process.env, fetcher: typeof fetch = fetch): SearchAdapter[] {
  const adapters: SearchAdapter[] = [];
  const id = env.NAVER_CLIENT_ID?.trim(), secret = env.NAVER_CLIENT_SECRET?.trim();
  const kakao = env.KAKAO_REST_API_KEY?.trim() || env.KAKAO_API_KEY?.trim();
  if (id && secret) adapters.push(adapter('naver', { 'X-NCP-APIGW-API-KEY-ID': id, 'X-NCP-APIGW-API-KEY': secret }, fetcher));
  if (kakao) adapters.push(adapter('kakao', { Authorization: `KakaoAK ${kakao}` }, fetcher));
  return adapters;
}

export type SearchAttempt = { provider: string; query: string; code: string };
export type SmartSearchResult = {
  provider: string | null; query: string | null; documents: SearchDocument[]; attempts: SearchAttempt[];
};

/** Exhaust each provider's smart queries before moving to the next provider. */
export async function searchPlace(params: SmartQueryParams, adapters = createSearchAdapters()): Promise<SmartSearchResult> {
  const attempts: SearchAttempt[] = [];
  const queries = buildSmartQueries(params);
  if (!queries.length) return { provider: null, query: null, documents: [], attempts };
  if (!adapters.length) throw new SearchError('missing_search_configuration');
  let succeeded = false;
  for (const source of adapters) {
    for (const query of queries) {
      try {
        const documents = await source.search(query);
        succeeded = true;
        attempts.push({ provider: source.name, query, code: documents.length ? 'found' : 'empty' });
        if (documents.length) return { provider: source.name, query, documents, attempts };
      } catch (error) {
        attempts.push({ provider: source.name, query, code: error instanceof SearchError ? error.code : 'unexpected_error' });
        // A provider failure (auth, quota, timeout, invalid payload) should not
        // repeat requests with weaker queries; move to the next provider.
        break;
      }
    }
  }
  if (!succeeded) throw new SearchError('all_search_providers_failed');
  return { provider: null, query: null, documents: [], attempts };
}
