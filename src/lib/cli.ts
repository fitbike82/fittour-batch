import { parseArgs } from 'node:util';
import { pathToFileURL } from 'node:url';
import { config } from 'dotenv';
import { BatchError, DailyBudget } from './budget.js';
import { database, SharedBudget, SupabaseStore } from './store.js';
import { TourApi } from './tour-api.js';

export const isMain = (url: string) => !!process.argv[1] && pathToFileURL(process.argv[1]).href === url;
export function options(args = process.argv.slice(2)) {
  const { values } = parseArgs({ args, options: {
    'env-file': { type: 'string' }, limit: { type: 'string', default: '500000' },
    'daily-limit': { type: 'string', default: '100000' }, 'already-used': { type: 'string', default: '0' },
    'max-seconds': { type: 'string', default: '7200' },
    'retry-failed': { type: 'boolean', default: false }, help: { type: 'boolean', default: false },
  } });
  if (values.help) return { help: true as const };
  const integer = (name: 'limit' | 'daily-limit' | 'already-used' | 'max-seconds', min: number, max: number) => {
    const raw = values[name]!;
    const value = Number(raw);
    if (!/^[0-9]+$/.test(raw) || !Number.isSafeInteger(value) || value < min || value > max) throw new BatchError(`invalid_${name}`);
    return value;
  };
  return { help: false as const, envFile: values['env-file'] ?? '.env',
    limit: integer('limit', 1, 500000), dailyLimit: integer('daily-limit', 1, 100000),
    alreadyUsed: integer('already-used', 0, 100000), maxSeconds: integer('max-seconds', 1, 86400),
    retryFailed: values['retry-failed']! };
}
export async function cli(job: (store: SupabaseStore, api: TourApi, opt: Exclude<ReturnType<typeof options>, { help: true }>) => Promise<object & { deferred?: boolean; failed?: number }>) {
  let budget: DailyBudget | undefined;
  try {
    const opt = options();
    if (opt.help) {
      console.log('Options: --env-file PATH --limit N --daily-limit 1..100000 --already-used N --max-seconds N --retry-failed');
      return;
    }
    const loaded = config({ path: opt.envFile, quiet: true });
    if (loaded.error && opt.envFile !== '.env') throw new BatchError('env_file_unavailable');
    const url = process.env.SUPABASE_URL ?? process.env.NEXT_PUBLIC_SUPABASE_URL;
    const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
    const tourKey = process.env.TOUR_API_DECODING_KEY;
    if (!url || !key || !tourKey) throw new BatchError('missing_server_configuration');
    const db = database(url, key);
    // Fail before spending provider quota when the migration has not been installed.
    const { data, error } = await db.rpc('batch_tour_preflight');
    if (error || data !== true) throw new BatchError('batch_tour_migration_required');
    budget = new DailyBudget(decodeURIComponent(tourKey.trim()), opt.dailyLimit, undefined, opt.alreadyUsed);
    const api = new TourApi(tourKey, new SharedBudget(db, tourKey, budget, opt.dailyLimit, opt.alreadyUsed));
    const started = performance.now();
    const result = await job(new SupabaseStore(db), api, opt);
    const seconds = (performance.now() - started) / 1000;
    console.log(JSON.stringify({ ...result, api_calls: api.calls, elapsed_seconds: Math.round(seconds),
      requests_per_second: Number((api.calls / Math.max(seconds, 0.001)).toFixed(2)) }));
    if (result.failed) process.exitCode = 1;
    else if (result.deferred) process.exitCode = 2;
  } catch (error) {
    console.error(JSON.stringify({ event: 'batch_stopped', code: error instanceof BatchError ? error.code : 'unexpected_error' }));
    process.exitCode = 1;
  } finally { budget?.close(); }
}
