import { createClient, type SupabaseClient } from '@supabase/supabase-js';
import { createHash } from 'node:crypto';
import { BatchError, QuotaStop, kstDay, type Budget } from './budget.js';
import type { Item } from './tour-api.js';
export type Counts = { inserted: number; updated: number; skipped: number; deactivated: number };
export type Place = { content_id: string; content_type_id: string; title: string; claim_token: string };
export type Details = { overview: string | null; intro: Item; course: Item | null; stops: Item[] };
export interface Store {
  master(rows: Item[]): Promise<Counts>;
  claim(limit: number, after: string, retryFailed: boolean): Promise<Place[]>;
  finish(place: Place, details: Details): Promise<boolean>;
  release(place: Place, failed: boolean): Promise<void>;
}
const recordError = (code?: string) => !!code && (/^22|^23/.test(code) || code === 'P0001');
export class SupabaseStore implements Store {
  constructor(public db: SupabaseClient, private log: (event: object) => void = console.log) {}
  async master(rows: Item[]): Promise<Counts> {
    const { data, error } = await this.db.rpc('sync_places_universe_batch', { p_rows: rows });
    if (!error) return data as Counts;
    if (!recordError(error.code)) throw new BatchError(`database_${error.code || 'unavailable'}`);
    if (rows.length === 1) {
      this.log({ event: 'master_record_skipped', content_id: rows[0].content_id, code: error.code });
      return { inserted: 0, updated: 0, skipped: 1, deactivated: 0 };
    }
    const mid = Math.floor(rows.length / 2);
    const a = await this.master(rows.slice(0, mid)), b = await this.master(rows.slice(mid));
    return { inserted: a.inserted + b.inserted, updated: a.updated + b.updated,
      skipped: a.skipped + b.skipped, deactivated: a.deactivated + b.deactivated };
  }
  async claim(limit: number, after: string, retryFailed: boolean) {
    const { data, error } = await this.db.rpc('claim_batch_tour_places', {
      p_limit: limit, p_after: after, p_retry_failed: retryFailed,
    });
    if (error) throw new BatchError(`database_claim_${error.code}`);
    return data as Place[];
  }
  async finish(place: Place, details: Details) {
    const { data, error } = await this.db.rpc('complete_batch_tour_place', {
      p_content_id: place.content_id, p_token: place.claim_token, p_overview: details.overview,
      p_intro: details.intro, p_course: details.course, p_stops: details.stops,
    });
    if (error) throw new BatchError(`${recordError(error.code) ? 'detail_rejected' : 'database_complete'}_${error.code}`);
    return data === true;
  }
  async release(place: Place, failed: boolean) {
    const { error } = await this.db.rpc('release_batch_tour_place', {
      p_content_id: place.content_id, p_token: place.claim_token, p_failed: failed,
    });
    if (error) throw new BatchError(`database_release_${error.code}`);
  }
}

export class SharedBudget implements Budget {
  private keyId: string;
  private initialDay = kstDay();
  constructor(private db: SupabaseClient, key: string, private local: Budget,
              private cap = 100_000, private alreadyUsed = 0) {
    this.keyId = createHash('sha256').update(decodeURIComponent(key.trim())).digest('hex');
  }
  async reserve() {
    await this.local.reserve();
    const { data, error } = await this.db.rpc('reserve_batch_tour_call', {
      p_key: this.keyId, p_cap: this.cap, p_floor: kstDay() === this.initialDay ? this.alreadyUsed : 0, p_block: false,
    });
    if (error) throw new BatchError(`database_quota_${error.code}`);
    if (!data) throw new QuotaStop();
  }
  async block() {
    await this.local.block();
    const { error } = await this.db.rpc('reserve_batch_tour_call', {
      p_key: this.keyId, p_cap: this.cap, p_floor: kstDay() === this.initialDay ? this.alreadyUsed : 0, p_block: true,
    });
    if (error) throw new BatchError(`database_quota_${error.code}`);
  }
}
export function database(url: string, key: string) {
  return createClient(url, key, { auth: { persistSession: false, autoRefreshToken: false },
    global: { fetch: (input, init) => fetch(input, { ...init, signal: AbortSignal.timeout(30_000) }) } });
}
