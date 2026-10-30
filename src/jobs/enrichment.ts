import pLimit from 'p-limit';
import { BatchError, QuotaStop } from '../lib/budget.js';
import { cli, isMain } from '../lib/cli.js';
import { CONTENT_TYPES, courseStops, sanitizeIntro, text } from '../lib/mapping.js';
import { allPages, type Page, type TourSource } from '../lib/tour-api.js';
import type { Store } from '../lib/store.js';

function validateDetail(page: Page): void {
  if (!page || !Array.isArray(page.items) || page.items.length !== 1 || page.total !== 1) {
    throw new BatchError('missing_or_ambiguous_detail');
  }
  const item = page.items[0];
  if (!item || typeof item !== 'object' || Array.isArray(item)) throw new BatchError('invalid_detail_item');
}

export async function enrich(store: Store, api: TourSource,
  options: { limit: number; retryFailed: boolean; maxSeconds: number; categories?: number[] } = { limit: 500000, retryFailed: false, maxSeconds: 7200 },
  log: (event: object) => void = console.log) {
  const counts = { claimed: 0, completed: 0, failed: 0, deferred: false };
  const deadline = Date.now() + options.maxSeconds * 1000;
  const workers = pLimit(10);
  let fatal: unknown;
  for (const category of options.categories ?? CONTENT_TYPES) {
    let after = '';
    while (counts.claimed < options.limit && !counts.deferred && !fatal) {
      if (Date.now() >= deadline) { counts.deferred = true; break; }
      const places = await store.claim(Math.min(100, options.limit - counts.claimed), after, options.retryFailed, [category]);
      if (!places.length) break;
      after = places[places.length - 1].content_id;
      counts.claimed += places.length;
      await Promise.all(places.map(place => workers(async () => {
        const release = async (failed: boolean, errorCode?: string) => {
          try { await store.release(place, failed, errorCode); }
          catch (error) { fatal ??= error; }
        };
        if (counts.deferred || fatal || Date.now() >= deadline) {
          counts.deferred = true;
          await release(false);
          return;
        }
        let stage = 'fetch';
        try {
          const params = { contentId: place.content_id, contentTypeId: place.content_type_id };
          // A separate HTTP limiter inside TourApi caps actual requests at 10,
          // even though each place fetches common/intro concurrently.
          // allSettled drains every request before releasing a claim after a failure.
          const payloads = await Promise.allSettled([
            api.page('detailCommon2', { contentId: place.content_id, pageNo: 1 }),
            api.page('detailIntro2', { ...params, pageNo: 1 }),
            place.content_type_id === '25' ? allPages(api, 'detailInfo2', params) : Promise.resolve([]),
          ]);
          const errors = payloads.filter(p => p.status === 'rejected').map(p => p.reason);
          if (errors.some(e => e instanceof QuotaStop)) throw new QuotaStop();
          if (errors.length) throw errors[0];
          const common = (payloads[0] as PromiseFulfilledResult<Awaited<ReturnType<TourSource['page']>>>).value;
          const intro = (payloads[1] as PromiseFulfilledResult<Awaited<ReturnType<TourSource['page']>>>).value;
          const stops = (payloads[2] as PromiseFulfilledResult<Awaited<ReturnType<typeof allPages>>>).value;
          stage = 'validate';
          validateDetail(common);
          validateDetail(intro);
          if (String(common.items[0].contentid) !== place.content_id ||
              (intro.items[0].contentid != null && String(intro.items[0].contentid) !== place.content_id)) {
            throw new BatchError('detail_identity_mismatch');
          }
          stage = 'map';
          const detail = sanitizeIntro(intro.items[0]);
          const course = place.content_type_id === '25' ? {
            title: text(common.items[0].title) ?? place.title,
            distance: text(detail.distance), taketime: text(detail.taketime), schedule: text(detail.schedule),
          } : null;
          if (course && !stops.length) throw new BatchError('empty_course');
          const details = { overview: text(common.items[0].overview), intro: detail,
            course, stops: course ? courseStops(stops) : [] };
          stage = 'finish';
          const saved = await store.finish(place, details);
          if (!saved) throw new BatchError('claim_expired_or_source_changed');
          counts.completed++;
        } catch (error) {
          if (error instanceof QuotaStop) {
            counts.deferred = true;
            await release(false);
          } else {
            const code = error instanceof BatchError ? error.code : 'unexpected_error';
            // Completion failures belong to this place. Claim/fetch infrastructure errors stop the run.
            const finishError = stage === 'finish' && /^database_/.test(code);
            if (!finishError && /^(database_|retryable_|provider_(20|30|31|32)|http_40[13]|claim_expired)/.test(code)) {
              fatal ??= error;
              await release(false);
            } else {
              counts.failed++;
              await release(true, code);
            }
            log({ event: 'enrichment_error', content_id: place.content_id, code, stage,
              error_type: error instanceof Error ? error.name : typeof error });
          }
        }
      })));
      log({ event: 'enrichment_progress', category, ...counts, ...(store.snapshot ? { database: await store.snapshot() } : {}) });
    }
  }
  if (fatal) throw fatal;
  return counts;
}
if (isMain(import.meta.url)) await cli((store, api, options) => enrich(store, api, options));
