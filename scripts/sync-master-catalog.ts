import { BatchError, QuotaStop } from '../src/lib/budget.js';
import { cli, isMain } from '../src/lib/cli.js';
import { CONTENT_TYPES, date8, mapMaster, text } from '../src/lib/mapping.js';
import type { Store } from '../src/lib/store.js';
import type { Item, TourSource } from '../src/lib/tour-api.js';

type MasterResult = {
  fetched: number; inserted: number; updated: number; deactivated: number;
  skipped: number; categories: number; unchanged: number; deferred: boolean;
  stop_reason?: string;
};

/** A one-off full catalog walk. No scheduler is registered. */
export async function syncMaster(store: Store, api: TourSource,
  log: (event: object) => void = console.log,
  options: { since?: string; categories?: number[]; active?: boolean; maxChanges?: number; deadline?: number } = {}): Promise<MasterResult> {
  const totals = { fetched: 0, inserted: 0, updated: 0, deactivated: 0, skipped: 0, categories: 0, unchanged: 0, deferred: false };
  const seen = new Set<string>();
  for (const category of options.categories ?? CONTENT_TYPES) {
    let consumed = 0, expected: number | undefined;
    for (let pageNo = 1; expected === undefined || consumed < expected; pageNo++) {
      if (options.deadline !== undefined && Date.now() >= options.deadline) {
        return { ...totals, deferred: true, stop_reason: 'time_budget' };
      }
      let page;
      try { page = await api.page(options.active === false ? 'areaBasedSyncList2' : 'areaBasedList2', {
        contentTypeId: category, pageNo, numOfRows: 1000, arrange: options.since ? 'C' : 'A',
        ...(options.active === false ? { showflag: '0', modifiedtime: options.since?.replaceAll('-', '') ?? '' } : {}),
      }); }
      catch (error) {
        if (error instanceof QuotaStop) return { ...totals, deferred: true, stop_reason: 'daily_quota_exhausted' };
        throw error;
      }
      if (expected !== undefined && page.total !== expected) throw new BatchError('catalog_population_changed');
      expected = page.total;
      if (!page.items.length && consumed < expected) throw new BatchError('catalog_incomplete_page');
      const rows: Item[] = [];
      for (const raw of page.items) {
        const id = text(raw.contentid);
        if (id && seen.has(id)) throw new BatchError('catalog_repeated_record');
        if (id) seen.add(id);
        const modified = text(raw.modifiedtime);
        if (options.since && modified && /^[0-9]{14}$/.test(modified) && date8(modified.slice(0, 8)) &&
            modified.slice(0, 8) < options.since.replaceAll('-', '')) {
          totals.unchanged++;
          continue;
        }
        try {
          if (options.active === false) {
            // Tombstones may omit title and coordinates. The existing RPC only
            // updates matching records, preserving their stored master fields.
            if (!id || !/^[0-9]{1,50}$/.test(id) || !modified || !/^[0-9]{14}$/.test(modified)
              || !date8(modified.slice(0, 8)) || Number(modified.slice(8, 10)) > 23
              || Number(modified.slice(10, 12)) > 59 || Number(modified.slice(12)) > 59) {
              throw new BatchError('invalid_tombstone_identity');
            }
            rows.push({ content_id: id, modified_time: modified, is_active: false });
          } else rows.push(mapMaster(raw, category));
        }
        catch (error) {
          if (!(error instanceof BatchError)) throw error;
          totals.skipped++;
          log({ event: 'catalog_record_skipped', content_id: id, category, page: pageNo, code: error.code });
        }
      }
      for (let start = 0; start < rows.length;) {
        if (options.deadline !== undefined && Date.now() >= options.deadline) {
          return { ...totals, deferred: true, stop_reason: 'time_budget' };
        }
        const remaining = (options.maxChanges ?? Infinity) - totals.inserted - totals.updated - totals.deactivated;
        if (remaining <= 0) return { ...totals, deferred: true, stop_reason: 'master_change_limit' };
        const chunk = rows.slice(start, start + Math.min(100, remaining));
        const counts = await store.master(chunk);
        start += chunk.length;
        totals.inserted += counts.inserted;
        totals.updated += counts.updated;
        totals.deactivated += counts.deactivated;
        totals.skipped += counts.skipped;
      }
      consumed += page.items.length;
      totals.fetched += page.items.length;
      if (consumed > expected) throw new BatchError('catalog_count_mismatch');
      log({ event: 'catalog_page', category, page: pageNo, fetched: consumed, total: expected });
    }
    totals.categories++;
  }
  return totals;
}
if (isMain(import.meta.url)) await cli((store, api, opt) => syncMaster(store, api, console.log, { categories: opt.categories }), 'master');
