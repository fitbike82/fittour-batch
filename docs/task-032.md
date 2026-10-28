# TASK-032: daily master and three enrichment relays

## Database rollout

Target: `qiuojsjorjnuedludklw`.

The PM-approved `20261028074529_batch_tour_enrichment.sql` has been applied to the
production schema. Verified: tour_courses, tour_course_places, batch_tour.daily_calls
have RLS enabled; active-parent course SELECT policies exist; the service-role REST
preflight returns true. Supabase security Advisor was read: it reports existing
mutable-search-path functions and private RLS tables with no visitor policy. New
batch functions set an empty search_path and private ledgers intentionally grant only
the service role access.

The implementation also needs `20261028084311_daily_tour_relay.sql`. It adds:

- A private, RLS-protected daily-master checkpoint table and service-only checkpoint RPCs.
- A filtered lease/claim RPC for category selection, using the original queue semantics.
- One coherent DB snapshot RPC for live totals, category statuses and the shared daily ledger.
- Compatibility flag handling: changed source revisions reset enrichment_completed;
  successful atomic detail commits set it true.

Both migrations passed embedded PostgreSQL tests, including repeated application,
RLS/privilege checks, rollback, category filters, counters, leases and daily ceilings.
The second migration has **not** been applied: automatic approval review restricted
production approval to the explicitly named first SQL. The attempted combined command
also included migration-history registration, and was rejected before execution.
No new SQL or migration-history entries from that command reached the database.
Request approval for the second SQL before deploying the new workflow code.

GitHub CLI authentication is currently invalid, and the connected GitHub account has
read access but no push permission to this repository. Workflow files are ready in
the local checkout; publishing requires a GitHub account with repository push access.

## Schedule and category policy

| KST | UTC cron | Command | Categories | Cumulative KST daily ceiling |
| --- | --- | --- | --- | ---: |
| 01:00 | `0 16 * * *` | `sync:daily-master` | all eight | 100000 |
| 02:00 | `0 17 * * *` | `batch-tour-enrichment` | 12,14,15,28 | 40000 |
| 04:00 | `0 19 * * *` | `batch-tour-enrichment` | 39,32,38,25 | 80000 |
| 06:00 | `0 21 * * *` | `batch-tour-enrichment --retry-failed` | all eight | 100000 |

All detail waves pass `--max-seconds 5400 --limit 25000`. Wave ceilings are cumulative,
not independent quotas. The master and all three waves share the same decoded-key
hash and KST day in the DB ledger. Exhausting 40000 in wave 1 does not block wave 2
from reserving calls up to 80000, nor wave 3 from reaching 100000. Provider daily
exhaustion blocks the entire remaining KST day. Unused headroom carries into later
waves; it does not reset per job. Retries and course pagination count as calls.

Separate schedule entries are equivalent to `0 17,19,21 * * *` and let the workflow
identify the intended wave from github.event.schedule even when a runner starts late.
The new master workflow replaces the old 00:30 Python schedule. Both new workflows
share a concurrency group. These schedules supersede the initial extraction gate for
Tour jobs; the AI summary gate is unchanged.

Each category is processed in configured priority order, with an independent keyset
cursor. This prevents a high-ID restaurant from hiding a lower-ID lodging/course
record when moving to the next category. The third wave retries failed records once
per invocation and can recover previous days' failures.

## Daily master semantics

areaBasedList2 does not provide a modifiedtime filter. The daily command requests
1000 rows/page for every category and filters parsed source modifiedtime client-side.
Default lower bound is yesterday in KST, inclusive. A saved last-successful day widens
that interval after missed runs. `--since YYYY-MM-DD` provides an explicit recovery
window. Scanning continues through all pages, including after an old row; response
ordering is not used to assume the rest of a category is old.

Upserts retain the existing source-revision and image-preservation rules. New rows
have enrichment_completed=false and enrich_status=NULL; source revision changes reset
both completion indicators. The next relay selects the NULL/new work. Category-filtered
manual scans do not advance the all-category checkpoint. Quota, incomplete scans or
system errors retain the checkpoint; corrupt records are individually logged/skipped.

## Manual controls and reports

workflow_dispatch exposes category, limit, runtime, wave and failed-retry controls.
Manual category=all means all eight categories; scheduled waves use the priority
groups above. Inputs are passed through environment variables/quoted shell arrays,
and the CLI validates category/date/integer values.

```sh
npm run sync:daily-master -- --env-file /path/to/server.env
npm run sync:daily-master -- --since 2026-10-25 --env-file /path/to/server.env
npm run batch-tour-enrichment -- --categories 39,32 --limit 100 --max-seconds 5400
npm run batch:report -- --env-file /path/to/server.env
```

Reports query the database before and after each command, and after each detail claim
batch. JSON logs and last-run.json include global master rows, active rows, selected
active denominator, completed/unprocessed/pending/failed/remaining counts, completion
percentage, per-category counts and the live KST ledger usage. Completed means this
new pipeline's enrich_status=completed. Legacy enrichment_completed counts are separate.
Summary percentages use the selected active DB population; an empty population yields
0%, and missing data is never replaced with a fixed denominator. The legacy manual
Python report also uses a live DB count rather than a static baseline.

GITHUB_STEP_SUMMARY receives a before/after table and per-run call/elapsed metrics.
Artifacts contain the JSON report and safe structured run log. No provider raw body,
key or request URL is exported. Exit 2 is a quota/runtime deferral, accepted as resumable
by Actions; exit 1 remains a workflow failure. The 105-minute job timeout permits the
90-minute collection window plus draining bounded retries and writing the final report.

## Verification

`npm run typecheck`, `npm test`, `python -m unittest`.

Node tests cover workflow schedules and cumulative caps, dispatch arguments, category
cursor reset, KST dates, catch-up checkpoints, old-row ordering, fixed-denominator
removal, filtered queues, completion flags, live totals, quotas and visitor privileges.
Production batch execution awaits the second migration and repository publication.
