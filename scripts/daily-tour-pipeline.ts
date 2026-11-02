import { BatchError } from '../src/lib/budget.js';
import { cli, isMain, type Options } from '../src/lib/cli.js';
import { enrich } from '../src/jobs/enrichment.js';
import type { Store } from '../src/lib/store.js';
import type { TourSource } from '../src/lib/tour-api.js';
import { syncDailyMaster } from './sync-daily-master.js';

export const DAILY_PLACE_CAP = 10_000;

export async function dailyTourPipeline(store: Store, api: TourSource,
  options: Pick<Options, 'limit' | 'maxSeconds' | 'categories' | 'retryFailed' | 'since'>,
  log: (event: object) => void = console.log) {
  if (!Number.isSafeInteger(options.limit) || options.limit < 1) throw new BatchError('invalid_limit');
  const limit = Math.min(options.limit, DAILY_PLACE_CAP);
  const deadline = Date.now() + options.maxSeconds * 1000;
  // One API client and one shared/local quota ledger span both phases.
  const master = await syncDailyMaster(store, api, {
    categories: options.categories, since: options.since, maxChanges: limit,
    deadline, includeInactive: true,
  }, log);
  const secondsLeft = (deadline - Date.now()) / 1000;
  // A change cap leaves a durable queue to drain; quota/runtime stops cannot
  // start new requests. The master checkpoint is only advanced after a full scan.
  const stop = master.deferred && master.stop_reason !== 'master_change_limit'
    && master.inactive?.stop_reason !== 'master_change_limit';
  if (stop || secondsLeft <= 0) {
    return { master, limit, claimed: 0, completed: 0, failed: 0, deferred: true,
      stop_reason: secondsLeft <= 0 ? 'time_budget' : master.inactive?.stop_reason ?? master.stop_reason };
  }
  log({ event: 'daily_enrichment_start', limit });
  const detail = await enrich(store, api, { ...options, limit, maxSeconds: secondsLeft }, log);
  const capped = detail.claimed >= limit;
  return { master, limit, ...detail, deferred: master.deferred || detail.deferred || capped,
    ...(detail.stop_reason ? {} : capped ? { stop_reason: 'place_limit' }
      : master.deferred ? { stop_reason: master.inactive?.stop_reason ?? master.stop_reason } : {}) };
}

if (isMain(import.meta.url)) await cli((store, api, options) => dailyTourPipeline(store, api, options), 'daily-tour');
