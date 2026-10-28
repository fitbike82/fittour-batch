import { BatchError, kstDay } from '../src/lib/budget.js';
import { cli, isMain } from '../src/lib/cli.js';
import type { Store } from '../src/lib/store.js';
import type { TourSource } from '../src/lib/tour-api.js';
import { CONTENT_TYPES } from '../src/lib/mapping.js';
import { syncMaster } from './sync-master-catalog.js';

export async function syncDailyMaster(store: Store, api: TourSource,
  options: { categories?: number[]; since?: string; now?: Date } = {}, log: (event: object) => void = console.log) {
  const day = kstDay(options.now);
  const yesterday = new Date(`${day}T00:00:00Z`);
  yesterday.setUTCDate(yesterday.getUTCDate() - 1);
  const defaultSince = yesterday.toISOString().slice(0, 10);
  if (!store.dailySince || !store.completeDaily) throw new BatchError('daily_master_store_required');
  const since = options.since ?? await store.dailySince(defaultSince);
  if (since > day) throw new BatchError('future_master_since');
  log({ event: 'daily_master_start', since, day });
  // areaBasedList2 does not expose a modifiedtime filter. Scan every page and
  // filter client-side; do not assume sort stability or silently truncate a category.
  const result = await syncMaster(store, api, log, { since, categories: options.categories });
  if (!result.deferred && result.categories === CONTENT_TYPES.length) await store.completeDaily(day);
  return { ...result, since, day };
}
if (isMain(import.meta.url)) await cli((store, api, opt) => syncDailyMaster(store, api, opt), 'daily-master');
