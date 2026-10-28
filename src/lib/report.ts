import { appendFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import type { Snapshot } from './store.js';

export type RunReport = {
  job: string; started_at: string; status: 'completed' | 'deferred' | 'failed';
  daily_limit?: number; place_limit?: number; categories?: number[]; before?: Snapshot; after?: Snapshot; result?: object;
  code?: string; api_calls?: number; elapsed_seconds?: number; requests_per_second?: number;
};
export function summary(report: RunReport): string {
  const lines = [`## FitTour ${report.job}: ${report.status}`, '',
    '| Live DB metric | Before | After |', '| --- | ---: | ---: |'];
  for (const [label, field] of [
    ['Master rows', 'database_total'], ['Active places (all categories)', 'active_total'],
    ['Selected active places', 'selected_total'], ['Completed', 'completed'],
    ['Remaining', 'remaining'], ['Unprocessed', 'unprocessed'], ['Pending leases', 'pending'],
    ['Failed', 'failed'], ['Completion %', 'completion_percent'],
  ] as const) lines.push(`| ${label} | ${report.before?.[field] ?? '-'} | ${report.after?.[field] ?? '-'} |`);
  lines.push(`| KST daily calls | ${report.before?.quota.used ?? '-'} | ${report.after?.quota.used ?? '-'} |`, '',
    `Cumulative daily ceiling: ${report.daily_limit ?? 100000}. Place limit: ${report.place_limit ?? '-'}.`, '',
    `Requests this run: ${report.api_calls ?? 0}. Elapsed seconds: ${report.elapsed_seconds ?? 0}.`, '');
  if (report.result) lines.push('```json', JSON.stringify(report.result), '```', '');
  if (report.code) lines.push(`Error code: ${report.code}`, '');
  return lines.join('\n');
}
export function saveReport(report: RunReport, path: string) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(report, null, 2) + '\n');
  if (process.env.GITHUB_STEP_SUMMARY) appendFileSync(process.env.GITHUB_STEP_SUMMARY, summary(report) + '\n');
}
