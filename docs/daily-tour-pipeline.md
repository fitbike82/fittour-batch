# Daily Tour incremental pipeline

Run `npm run sync:daily-tour -- --max-seconds 5400` with the existing server-only
Supabase and TourAPI environment variables. This command shares one API client,
local journal and atomic KST database quota ledger across master and detail work.
No tables, columns or migrations are added.

The existing `daily_master_since` checkpoint supplies the overlapping date window.
Active master pages still use the established complete `areaBasedList2` walk with
client-side modification-date filtering: do not silently truncate a category or
assume that endpoint has a modification filter. Inactive changes use
`areaBasedSyncList2` with `showflag=0` and the same `modifiedtime` date window,
matching the repository's existing Python sync implementation. Tombstones can
omit title/coordinates and only deactivate matching records through the existing
`sync_places_universe_batch` RPC; records absent from a list are never assumed
deleted. Provider population/repetition and malformed-response checks remain.

Master writes stay at most 100 records per transaction. Actual inserts, updates
and deactivations together are capped at 10,000 per run (or a lower requested
limit). Replayed unchanged records do not consume this change allowance. A cap,
quota or runtime stop retains the checkpoint so unvisited changes are not lost.
The next run replays safely through the existing revision-aware upsert and moves
past already committed rows. A successful scan of all eight categories and both
active/inactive streams advances the master checkpoint, independently of pending
enrichment: the detail queue itself is durable. This requires the existing RPC's
unchanged-row skip behavior, not an unconditional upsert.

Existing DB triggers on `modified_time`/`content_type_id` invalidate
`enrich_status`, `enrichment_completed` and old claims when a source revision
changes. Newly inserted records start incomplete. Detail collection then claims
only active incomplete records in existing category/keyset order, in batches of
at most 100, with HTTP concurrency ten. Failed records are included only with
`--retry-failed`; a new source revision makes a failed record eligible normally.
The daily wrapper hard-clamps detail claims to 10,000 even if a caller requests
more; bulk commands retain their manual recovery limits. Invalidating a revision
does not erase the old detail fields before a successful replacement.

The cap is per phase, not the number of catalog rows inspected or API requests.
No more than 10,000 master changes and 10,000 detail claims occur in one run.
The master can inspect the full catalog to discover changes. More than 10,000
detail records stay queued until later runs. The report exposes the effective
limit, nested master/inactive outcomes and detail counts. Reaching the claim cap
conservatively reports `deferred` / `place_limit`, even if that final batch
happened to drain the queue. Exit 2 signals a resumable boundary, not data loss.
Per-place failures produce exit 1 for the combined daily command.

A single deadline bounds both phases. No detail phase starts after master
quota/runtime exhaustion; a master *change cap* still allows draining its saved
detail queue within the remaining deadline. In-flight requests retain existing
timeouts and backoff, and all requests/retries reserve the shared daily budget
(maximum 100,000). A 10,000-place cap is not itself a guarantee of staying under
quota when course pagination or retries are unusually large; the ledger enforces
the independent request ceiling. Existing lease, release and claim-retry rules
are reused.

`.github/workflows/daily_places_sync.yml` runs the combined pipeline at 01:00 KST
with a 90-minute work budget and 105-minute workflow timeout. Its existing
concurrency group serializes it with manual bulk relays. The old three daily
bulk schedules are removed, and manual full-catalog/bulk recovery remains
available. Deploy the repository changes to activate this schedule; no live run
or production deployment is performed as part of local testing.

Validation includes existing migration integration coverage for source revision
invalidation, new pipeline sequencing/tombstone/quota tests, capped-master replay,
and an actual 10,001-item in-memory queue proving 10,000 claims and one remaining.
