import { parseArgs } from 'node:util';
import { pathToFileURL } from 'node:url';
import { config } from 'dotenv';
import { BatchError, DailyBudget } from './budget.js';
import { database, keyId, SharedBudget, SupabaseStore } from './store.js';
import { TourApi } from './tour-api.js';
import { CONTENT_TYPES } from './mapping.js';
import { saveReport, type RunReport } from './report.js';
import { optionalSnapshot } from './snapshot.js';

export const isMain = (url: string) => !!process.argv[1] && pathToFileURL(process.argv[1]).href === url;
export function options(args = process.argv.slice(2)) {
  const { values } = parseArgs({ args, options: {
    'env-file': { type: 'string' }, limit: { type: 'string', default: '500000' },
    'daily-limit': { type: 'string', default: '100000' }, 'already-used': { type: 'string', default: '0' },
    'max-seconds': { type: 'string', default: '7200' }, categories: { type: 'string', default: 'all' },
    since: { type: 'string' }, report: { type: 'string', default: 'artifacts/tour-batch/last-run.json' },
    'retry-failed': { type: 'boolean', default: false }, help: { type: 'boolean', default: false },
  } });
  if (values.help) return { help: true as const };
  const integer = (name: 'limit' | 'daily-limit' | 'already-used' | 'max-seconds', min: number, max: number) => {
    const raw = values[name]!;
    const value = Number(raw);
    if (!/^[0-9]+$/.test(raw) || !Number.isSafeInteger(value) || value < min || value > max) throw new BatchError(`invalid_${name}`);
    return value;
  };
  const categories = values.categories === 'all' ? [...CONTENT_TYPES] : values.categories!.split(',').map(value => {
    if (!/^[0-9]+$/.test(value) || !(CONTENT_TYPES as readonly number[]).includes(Number(value))) throw new BatchError('invalid_categories');
    return Number(value);
  });
  if (new Set(categories).size !== categories.length) throw new BatchError('duplicate_categories');
  if (values.since && (!/^\d{4}-\d{2}-\d{2}$/.test(values.since) ||
    !Number.isFinite(new Date(`${values.since}T00:00:00Z`).getTime()) ||
    new Date(`${values.since}T00:00:00Z`).toISOString().slice(0, 10) !== values.since)) throw new BatchError('invalid_since');
  return { help: false as const, envFile: values['env-file'] ?? '.env', categories, since: values.since,
    report: values.report!, limit: integer('limit', 1, 500000), dailyLimit: integer('daily-limit', 1, 100000),
    alreadyUsed: integer('already-used', 0, 100000), maxSeconds: integer('max-seconds', 1, 86400),
    retryFailed: values['retry-failed']! };
}
export type Options = Exclude<ReturnType<typeof options>, { help: true }>;
export function configuredStore(opt: Options) {
  const loaded = config({ path: opt.envFile, quiet: true });
  if (loaded.error && opt.envFile !== '.env') throw new BatchError('env_file_unavailable');
  const url = process.env.SUPABASE_URL ?? process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  const tourKey = process.env.TOUR_API_DECODING_KEY;
  if (!url || !key || !tourKey) throw new BatchError('missing_server_configuration');
  const db = database(url, key);
  return { db, tourKey, store: new SupabaseStore(db, console.log, keyId(tourKey), opt.categories) };
}
export async function cli(job: (store: SupabaseStore, api: TourApi, opt: Options) => Promise<object & { deferred?: boolean; failed?: number }>, name = 'enrichment') {
  let budget: DailyBudget | undefined, store: SupabaseStore | undefined, api: TourApi | undefined, opt: Options | undefined;
  const started = performance.now();
  const report: RunReport = { job: name, started_at: new Date().toISOString(), status: 'failed' };
  try {
    const parsed = options();
    if (parsed.help) {
      console.log('Options: --env-file PATH --categories all|12,14,... --limit N --daily-limit 1..100000 --already-used N --max-seconds N --retry-failed --since YYYY-MM-DD --report PATH');
      return;
    }
    opt = parsed;
    report.categories = opt.categories; report.daily_limit = opt.dailyLimit; report.place_limit = opt.limit;
    const configured = configuredStore(opt);
    store = configured.store;
    const { data, error } = await configured.db.rpc('batch_tour_relay_preflight');
    if (error || data !== true) throw new BatchError('batch_tour_relay_migration_required');
    report.before = await optionalSnapshot(store, 'before');
    budget = new DailyBudget(decodeURIComponent(configured.tourKey.trim()), opt.dailyLimit, undefined, opt.alreadyUsed);
    api = new TourApi(configured.tourKey, new SharedBudget(configured.db, configured.tourKey, budget, opt.dailyLimit, opt.alreadyUsed));
    const result = await job(store, api, opt);
    report.result = result;
    // Enrichment record failures are accounted for in result.failed; a drained
    // queue is still a successful batch. Fatal errors take the catch path.
    const failed = name !== 'enrichment' && !!result.failed;
    report.status = failed ? 'failed' : result.deferred ? 'deferred' : 'completed';
    process.exitCode = failed ? 1 : result.deferred ? 2 : 0;
  } catch (error) {
    report.code = error instanceof BatchError ? error.code : 'unexpected_error';
    console.error(JSON.stringify({ event: 'batch_stopped', code: report.code }));
    process.exitCode = 1;
  } finally {
    budget?.close();
    if (opt) {
      if (store) report.after = await optionalSnapshot(store, 'after');
      const seconds = (performance.now() - started) / 1000;
      report.api_calls = api?.calls ?? 0; report.elapsed_seconds = Math.round(seconds);
      report.requests_per_second = Number((report.api_calls / Math.max(seconds, 0.001)).toFixed(2));
      console.log(JSON.stringify({ event: 'batch_report', ...report }));
      saveReport(report, opt.report);
    }
  }
}
