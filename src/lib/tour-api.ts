import pLimit from 'p-limit';
import { BatchError, QuotaStop, type Budget } from './budget.js';

export type Item = Record<string, unknown>;
export type Page = { items: Item[]; total: number };
export interface TourSource { page(endpoint: string, params: Record<string, string | number>): Promise<Page>; }
const wait = (ms: number) => new Promise<void>(r => setTimeout(r, ms));

export class TourApi implements TourSource {
  private limit = pLimit(10);
  calls = 0;
  constructor(private key: string, private budget: Budget, private fetcher: typeof fetch = fetch,
              private sleep = wait) {}
  async page(endpoint: string, params: Record<string, string | number>): Promise<Page> {
    for (let attempt = 0; ; attempt++) {
      try {
        return await this.limit(async () => {
          await this.budget.reserve();
          this.calls++;
          const url = new URL(`https://apis.data.go.kr/B551011/KorService2/${endpoint}`);
          // Accept either the decoded or once-encoded data.go.kr service key.
          let key = this.key.trim();
          try { key = decodeURIComponent(key); } catch { throw new BatchError('invalid_service_key_encoding'); }
          for (const [k, v] of Object.entries({ serviceKey: key, MobileOS: 'ETC',
            MobileApp: 'FitTourBatch', _type: 'json', numOfRows: 1000, ...params })) url.searchParams.set(k, String(v));
          let response: Response;
          try { response = await this.fetcher(url, { signal: AbortSignal.timeout(20_000) }); }
          catch { throw new BatchError('retryable_transport'); }
          if (response.status === 429 || response.status >= 500) throw new BatchError('retryable_http');
          if (!response.ok) throw new BatchError(`http_${response.status}`);
          let payload;
          try {
            const text = await response.text();
            if (text.trim().startsWith('<')) {
              const code = text.match(/<(?:returnReasonCode|resultCode)>([^<]+)</)?.[1];
              if (code === '22') { await this.budget.block(); throw new QuotaStop(); }
              throw new BatchError(code ? `provider_${code}` : 'invalid_xml_response');
            }
            payload = JSON.parse(text).response;
          } catch (error) {
            if (error instanceof BatchError) throw error;
            throw new BatchError('invalid_json_response');
          }
          const code = String(payload?.header?.resultCode);
          if (!['0000', '00', '0'].includes(code)) {
            if (code === '22') { await this.budget.block(); throw new QuotaStop(); }
            throw new BatchError(code === '23' ? 'retryable_provider_rate' : `provider_${code}`);
          }
          const body = payload?.body;
          const total = Number(body?.totalCount);
          if (body?.totalCount == null || !Number.isSafeInteger(total) || total < 0) throw new BatchError('invalid_page_total');
          let items = body?.items?.item ?? [];
          if (items && typeof items === 'object' && !Array.isArray(items)) items = [items];
          if (!Array.isArray(items) || items.some(i => !i || typeof i !== 'object' || Array.isArray(i))) {
            throw new BatchError('invalid_page_items');
          }
          if ((items.length === 0 && total > 0) || items.length > total) throw new BatchError('incomplete_page');
          return { items, total };
        });
      } catch (error) {
        if (!(error instanceof BatchError) || !error.code.startsWith('retryable_') || attempt >= 3) throw error;
        await this.sleep(500 * 2 ** attempt);
      }
    }
  }
}

export async function allPages(api: TourSource, endpoint: string, params: Record<string, string | number>): Promise<Item[]> {
  const rows: Item[] = [];
  let expected: number | undefined;
  for (let pageNo = 1; expected === undefined || rows.length < expected; pageNo++) {
    const page = await api.page(endpoint, { ...params, pageNo, numOfRows: 1000 });
    if (expected !== undefined && expected !== page.total) throw new BatchError('population_changed');
    expected = page.total;
    if (!page.items.length && rows.length < expected) throw new BatchError('incomplete_page');
    rows.push(...page.items);
    if (rows.length > expected) throw new BatchError('page_count_mismatch');
  }
  return rows;
}
