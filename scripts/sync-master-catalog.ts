import { BatchError, QuotaStop } from '../src/lib/budget.js';
import { cli, isMain } from '../src/lib/cli.js';
import { CONTENT_TYPES, date8, mapMaster, text } from '../src/lib/mapping.js';
import type { Store } from '../src/lib/store.js';
import type { Item, TourSource } from '../src/lib/tour-api.js';

/** A one-off full catalog walk. No scheduler is registered. */
export async function syncMaster(store: Store, api: TourSource,
  log: (event: object) => void = console.log, options: { since?: string; categories?: number[] } = {}) {
  const totals = { fetched: 0, inserted: 0, updated: 0, skipped: 0, categories: 0, unchanged: 0, deferred: false };
  const seen = new Set<string>();
  for (const category of options.categories ?? CONTENT_TYPES) {
    let consumed = 0, expected: number | undefined;
    for (let pageNo = 1; expected === undefined || consumed < expected; pageNo++) {
      let page;
      try { page = await api.page('areaBasedList2', { contentTypeId: category, pageNo, numOfRows: 1000, arrange: options.since ? 'C' : 'A' }); }
      catch (error) { if (error instanceof QuotaStop) return { ...totals, deferred: true }; throw error; }
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
        try { rows.push(mapMaster(raw, category)); }
        catch (error) {
          if (!(error instanceof BatchError)) throw error;
          totals.skipped++;
          log({ event: 'catalog_record_skipped', content_id: id, category, page: pageNo, code: error.code });
        }
      }
      for (let start = 0; start < rows.length; start += 100) {
        const counts = await store.master(rows.slice(start, start + 100));
        totals.inserted += counts.inserted;
        totals.updated += counts.updated;
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
