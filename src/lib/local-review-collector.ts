import { mkdir, readFile, rename, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';

export type Place = { content_id: string; title: string; sido: string; sigungu: string; addr1: string; pv_30d: number | null };
export type RawSource = { provider: 'naver' | 'kakao'; title: string; url: string; mobile_url: string; snippet: string };
export type SearchProvider = (query: string) => Promise<RawSource[]>;
export type SearchCredentials = {
  kakaoApiKey: string;
  naverClientId?: string;
  naverClientSecret?: string;
};
export type CollectionFile = {
  content_id: string; place_title: string; sido: string; sigungu: string; addr1: string;
  pv_30d: number | null; collected_at: string; sources: RawSource[];
};

const REQUEST_TIMEOUT_MS = 15_000;
const RETRY_DELAYS_MS = [500, 1_000, 2_000];
const REQUIRED_CSV_COLUMNS = ['content_id', 'title', 'sido', 'sigungu', 'addr1', 'pv_30d'] as const;

export function loadSearchCredentials(environ: NodeJS.ProcessEnv = process.env): SearchCredentials {
  const kakaoApiKey = environ.KAKAO_REST_API_KEY?.trim() || environ.KAKAO_API_KEY?.trim();
  if (!kakaoApiKey) throw new Error('missing environment variable: KAKAO_REST_API_KEY or KAKAO_API_KEY');
  const naverClientId = environ.NAVER_CLIENT_ID?.trim();
  const naverClientSecret = environ.NAVER_CLIENT_SECRET?.trim();
  return {
    kakaoApiKey,
    ...(naverClientId && naverClientSecret ? { naverClientId, naverClientSecret } : {}),
  };
}

function joined(...parts: Array<string | null | undefined>): string {
  return parts.map(part => part?.trim()).filter(Boolean).join(' ');
}

export function buildSearchQueries(place: Place): { primary: string; fallback: string } {
  const title = place.title.trim();
  if (!title) throw new Error(`place ${place.content_id} has no title`);
  return { primary: joined(place.sido, place.sigungu, title), fallback: joined(place.sigungu, title, '후기') };
}

function decodeHtml(value: string): string {
  const entities: Record<string, string> = { amp: '&', quot: '"', apos: "'", lt: '<', gt: '>', nbsp: ' ' };
  return value
    .replace(/&#(x[0-9a-f]+|\d+);/gi, (_, code: string) => {
      const numeric = code[0].toLowerCase() === 'x' ? Number.parseInt(code.slice(1), 16) : Number.parseInt(code, 10);
      return Number.isFinite(numeric) ? String.fromCodePoint(numeric) : '';
    })
    .replace(/&([a-z]+);/gi, (match, name: string) => entities[name.toLowerCase()] ?? match);
}

export function cleanSearchText(value: string): string {
  return decodeHtml(value).replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim();
}

function canonicalUrl(value: string): string | null {
  try {
    const parsed = new URL(value.trim());
    if (!['http:', 'https:'].includes(parsed.protocol)) return null;
    parsed.hash = '';
    return parsed.toString();
  } catch { return null; }
}

function naverMobileUrl(value: string): string {
  try {
    const url = new URL(value);
    if (url.hostname === 'blog.naver.com') url.hostname = 'm.blog.naver.com';
    return url.toString();
  } catch { return value; }
}

export function mergeUniqueSources(...groups: RawSource[][]): RawSource[] {
  const merged = new Map<string, RawSource>();
  for (const source of groups.flat()) {
    const url = canonicalUrl(source.url);
    if (!url || merged.has(url)) continue;
    merged.set(url, { ...source, url, mobile_url: canonicalUrl(source.mobile_url) ?? url });
  }
  return [...merged.values()];
}

function retryableStatus(status: number): boolean { return status === 408 || status === 429 || status >= 500; }

class SearchHttpError extends Error {
  constructor(readonly status: number) {
    super(`search request failed (${status})`);
  }
}

async function fetchJson<T>(url: URL, headers: Record<string, string>): Promise<T> {
  for (let attempt = 0; ; attempt++) {
    let response: Response;
    try { response = await fetch(url, { headers, signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) }); }
    catch (error) {
      if (attempt >= RETRY_DELAYS_MS.length) throw error;
      await new Promise(resolve => setTimeout(resolve, RETRY_DELAYS_MS[attempt]));
      continue;
    }
    if (response.ok) return await response.json() as T;
    if (!retryableStatus(response.status) || attempt >= RETRY_DELAYS_MS.length) {
      throw new SearchHttpError(response.status);
    }
    await new Promise(resolve => setTimeout(resolve, RETRY_DELAYS_MS[attempt]));
  }
}

export function withRequestDelay(provider: SearchProvider, delayMs = 200): SearchProvider {
  let queue = Promise.resolve();
  let lastStartedAt = 0;
  return query => {
    const request = queue.then(async () => {
      const waitMs = Math.max(0, delayMs - (Date.now() - lastStartedAt));
      if (waitMs) await new Promise(resolve => setTimeout(resolve, waitMs));
      lastStartedAt = Date.now();
      return provider(query);
    });
    queue = request.then(() => undefined, () => undefined);
    return request;
  };
}

export function createNaverSearch(clientId?: string, clientSecret?: string): SearchProvider {
  let disabled = !clientId?.trim() || !clientSecret?.trim();
  return async query => {
    if (disabled) return [];
    const url = new URL('https://naverapihub.apigw.ntruss.com/search/v1/blog');
    url.searchParams.set('query', query); url.searchParams.set('display', '5');
    url.searchParams.set('format', 'json');
    let payload: { items?: Array<{ title?: string; link?: string; description?: string }> };
    try {
      payload = await fetchJson(url, {
        'X-NCP-APIGW-API-KEY-ID': clientId!, 'X-NCP-APIGW-API-KEY': clientSecret!,
      });
    } catch (error) {
      if (error instanceof SearchHttpError && (error.status === 401 || error.status === 403)) {
        disabled = true;
        return [];
      }
      throw error;
    }
    return (payload.items ?? []).map(item => {
      const resultUrl = item.link ?? '';
      return { provider: 'naver', title: cleanSearchText(item.title ?? ''), url: resultUrl,
        mobile_url: naverMobileUrl(resultUrl), snippet: cleanSearchText(item.description ?? '') };
    });
  };
}

export function createKakaoSearch(apiKey: string): SearchProvider {
  return async query => {
    const url = new URL('https://dapi.kakao.com/v2/search/blog');
    url.searchParams.set('query', query); url.searchParams.set('size', '5');
    const payload = await fetchJson<{ documents?: Array<{ title?: string; url?: string; contents?: string }> }>(url, {
      Authorization: `KakaoAK ${apiKey}`,
    });
    return (payload.documents ?? []).map(item => ({ provider: 'kakao', title: cleanSearchText(item.title ?? ''),
      url: item.url ?? '', mobile_url: item.url ?? '', snippet: cleanSearchText(item.contents ?? '') }));
  };
}

export async function collectPlaceSources(place: Place, naverSearch: SearchProvider, kakaoSearch: SearchProvider): Promise<RawSource[]> {
  const queries = buildSearchQueries(place);
  const primary = await Promise.all([naverSearch(queries.primary), kakaoSearch(queries.primary)]);
  let sources = mergeUniqueSources(...primary);
  if (sources.length < 4) {
    const fallback = await Promise.all([naverSearch(queries.fallback), kakaoSearch(queries.fallback)]);
    sources = mergeUniqueSources(sources, ...fallback);
  }
  return sources;
}

function parseCsvRows(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [], field = '', quoted = false;
  for (let index = 0; index < text.length; index++) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') { field += '"'; index++; }
      else if (char === '"') quoted = false;
      else field += char;
    } else if (char === '"') quoted = true;
    else if (char === ',') { row.push(field); field = ''; }
    else if (char === '\n') { row.push(field); rows.push(row); row = []; field = ''; }
    else if (char !== '\r') field += char;
  }
  if (quoted) throw new Error('CSV contains an unterminated quoted field');
  if (field || row.length) { row.push(field); rows.push(row); }
  return rows;
}

function decodeCsv(buffer: Buffer): string {
  const offset = buffer[0] === 0xef && buffer[1] === 0xbb && buffer[2] === 0xbf ? 3 : 0;
  const bytes = buffer.subarray(offset);
  try { return new TextDecoder('utf-8', { fatal: true }).decode(bytes); }
  catch { return new TextDecoder('euc-kr', { fatal: true }).decode(bytes).replace(/^\uFEFF/, ''); }
}

function csvValue(value: string | undefined): string {
  const normalized = (value ?? '').trim();
  return normalized.toLowerCase() === 'null' ? '' : normalized;
}

export async function readPlacesCsv(filePath: string): Promise<Place[]> {
  const rows = parseCsvRows(decodeCsv(await readFile(filePath)));
  if (!rows.length) throw new Error('CSV is empty');
  const headers = rows[0].map(header => header.replace(/^\uFEFF/, '').trim());
  for (const column of REQUIRED_CSV_COLUMNS) if (!headers.includes(column)) throw new Error(`CSV is missing required column: ${column}`);
  const index = Object.fromEntries(headers.map((header, position) => [header, position])) as Record<string, number>;
  return rows.slice(1).filter(row => row.some(Boolean)).map((row, rowIndex) => {
    const contentId = csvValue(row[index.content_id]), title = csvValue(row[index.title]);
    if (!contentId || !title) throw new Error(`CSV row ${rowIndex + 2} is missing content_id or title`);
    const pv = csvValue(row[index.pv_30d]), pv30d = pv === '' ? null : Number(pv);
    if (pv30d !== null && !Number.isFinite(pv30d)) throw new Error(`CSV row ${rowIndex + 2} has invalid pv_30d`);
    return { content_id: contentId, title, sido: csvValue(row[index.sido]), sigungu: csvValue(row[index.sigungu]),
      addr1: csvValue(row[index.addr1]), pv_30d: pv30d };
  });
}

export function toCollectionFile(place: Place, sources: RawSource[], collectedAt = new Date()): CollectionFile {
  return { content_id: place.content_id, place_title: place.title, sido: place.sido, sigungu: place.sigungu,
    addr1: place.addr1, pv_30d: place.pv_30d, collected_at: collectedAt.toISOString(), sources };
}

export function collectionFilePath(outputDirectory: string, contentId: string): string {
  if (!/^\d+$/.test(contentId)) throw new Error(`invalid content_id: ${contentId}`);
  return path.join(outputDirectory, `${contentId}.json`);
}

export async function hasNonEmptyCollectionFile(outputDirectory: string, contentId: string): Promise<boolean> {
  try { return (await stat(collectionFilePath(outputDirectory, contentId))).size > 0; }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return false;
    throw error;
  }
}

export async function saveCollectionFile(outputDirectory: string, payload: CollectionFile): Promise<string> {
  await mkdir(outputDirectory, { recursive: true });
  const filePath = collectionFilePath(outputDirectory, payload.content_id);
  const temporaryPath = `${filePath}.${process.pid}.${Date.now()}.tmp`;
  await writeFile(temporaryPath, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
  await rename(temporaryPath, filePath);
  return filePath;
}
