import { readFile, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { load } from 'cheerio';

export const MAX_FULL_TEXT_LENGTH = 50_000;
export const MIN_VALID_FULL_TEXT_LENGTH = 101;
export const LEGACY_RESCRAPE_THRESHOLD = 1_000;
export const MOBILE_USER_AGENT = 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36';
const NON_ARTICLE_SELECTORS = 'script,style,noscript,nav,header,footer,aside,form,button,svg,canvas,[role="navigation"],.nav,.navigation,.menu,.sidebar';

export type ScrapeSource = {
  provider?: string;
  title?: string;
  url?: string;
  mobile_url?: string;
  snippet?: string;
  full_text?: string;
  char_count?: number;
  scrape_status?: 'scraped' | 'failed';
  scrape_max_chars?: number;
  [key: string]: unknown;
};

export type RawSourceFile = {
  content_id: string;
  sources: ScrapeSource[];
  [key: string]: unknown;
};

export function truncateText(value: string): string {
  if (value.length <= MAX_FULL_TEXT_LENGTH) return value;
  let result = value.slice(0, MAX_FULL_TEXT_LENGTH);
  const final = result.charCodeAt(result.length - 1);
  if (final >= 0xd800 && final <= 0xdbff) result = result.slice(0, -1);
  return result;
}

export function cleanBodyText(value: string): string {
  return truncateText(value
    .replace(/\u00a0/g, ' ')
    .replace(/[\t\f\v ]+/g, ' ')
    .replace(/ *\n */g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim());
}

export function normalizeScrapeUrl(source: ScrapeSource): string {
  const candidate = String(source.mobile_url || source.url || '').trim();
  if (!candidate) throw new Error('missing source URL');
  const url = new URL(candidate);
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error('unsupported source URL');
  if (url.hostname === 'blog.naver.com' || url.hostname === 'm.blog.naver.com') {
    const blogId = url.searchParams.get('blogId');
    const logNo = url.searchParams.get('logNo');
    if (blogId && logNo) return `https://m.blog.naver.com/${encodeURIComponent(blogId)}/${encodeURIComponent(logNo)}`;
    const segments = url.pathname.split('/').filter(Boolean);
    if (segments.length >= 2) return `https://m.blog.naver.com/${segments[0]}/${segments[1]}`;
  }
  url.hash = '';
  return url.toString();
}

function elementText(element: ReturnType<ReturnType<typeof load>>): string {
  const clone = element.clone();
  clone.find(NON_ARTICLE_SELECTORS).remove();
  clone.find('br').replaceWith('\n');
  clone.find('p,div,section,li,h1,h2,h3,h4,h5,h6,blockquote').append('\n');
  return cleanBodyText(clone.text());
}

export function extractArticleText(html: string, sourceUrl: string): string {
  const $ = load(html);
  $(NON_ARTICLE_SELECTORS).remove();
  const host = new URL(sourceUrl).hostname;
  const selectors = host === 'blog.naver.com' || host === 'm.blog.naver.com'
    ? ['div.se-main-container', 'div#viewTypeSelector']
    : ['article', 'div.tt_article_useless_p_margin', 'main'];
  for (const selector of selectors) {
    const candidates = $(selector).toArray()
      .map(node => elementText($(node)))
      .sort((left, right) => right.length - left.length);
    if (candidates[0]?.length >= MIN_VALID_FULL_TEXT_LENGTH) return candidates[0];
  }
  const paragraphs = $('p').toArray().map(node => cleanBodyText($(node).text())).filter(Boolean);
  const fallback = cleanBodyText(paragraphs.join('\n'));
  if (fallback.length >= MIN_VALID_FULL_TEXT_LENGTH) return fallback;
  throw new Error('article body too short or unavailable');
}

export function hasValidFullText(source: ScrapeSource): boolean {
  return source.scrape_status === 'scraped'
    && typeof source.full_text === 'string'
    && source.full_text.length > 100
    && typeof source.char_count === 'number'
    && source.char_count === source.full_text.length
    && (source.full_text.length >= LEGACY_RESCRAPE_THRESHOLD
      || source.scrape_max_chars === MAX_FULL_TEXT_LENGTH);
}

export async function scrapeSource(source: ScrapeSource, fetcher: typeof fetch = fetch): Promise<ScrapeSource> {
  if (hasValidFullText(source)) return source;
  try {
    const url = normalizeScrapeUrl(source);
    const response = await fetcher(url, {
      headers: { 'User-Agent': MOBILE_USER_AGENT, Accept: 'text/html,application/xhtml+xml' },
      signal: AbortSignal.timeout(15_000),
      redirect: 'follow',
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const contentType = response.headers.get('content-type') ?? '';
    if (contentType && !contentType.includes('text/html') && !contentType.includes('application/xhtml+xml')) {
      throw new Error('unsupported response content type');
    }
    const fullText = extractArticleText(await response.text(), response.url || url);
    return { ...source, full_text: fullText, char_count: fullText.length,
      scrape_status: 'scraped', scrape_max_chars: MAX_FULL_TEXT_LENGTH };
  } catch {
    const fullText = cleanBodyText(load(String(source.snippet ?? '')).text());
    return { ...source, full_text: fullText, char_count: fullText.length, scrape_status: 'failed' };
  }
}

export async function readRawSourceFile(filePath: string): Promise<RawSourceFile> {
  const payload = JSON.parse(await readFile(filePath, 'utf8')) as unknown;
  if (!payload || typeof payload !== 'object' || !Array.isArray((payload as RawSourceFile).sources)) {
    throw new Error(`invalid raw source file: ${path.basename(filePath)}`);
  }
  return payload as RawSourceFile;
}

export async function saveRawSourceFile(filePath: string, payload: RawSourceFile): Promise<void> {
  const temporaryPath = `${filePath}.${process.pid}.${Date.now()}.tmp`;
  await writeFile(temporaryPath, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
  await rename(temporaryPath, filePath);
}
