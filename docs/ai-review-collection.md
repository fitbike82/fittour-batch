# AI review collection

The scheduled review job claims up to 50 places, with a 30 minute runtime budget.
For each place it loads the address from `places` if the queue omitted region
names, and searches the province/district plus place name with up to three
suffixes: 방문 후기, 솔직 후기, 내돈내산.

Each round calls Naver blog, Naver cafearticle, and Kakao blog search concurrently.
Each endpoint returns up to five candidates. Public readable Naver blog and cafe
articles share five source slots; Kakao has five slots. Naver blog/cafe candidates
are interleaved. Duplicate URLs across providers are counted only once. Bad
articles do not invalidate the place: subsequent candidates/rounds replace them.
Collection stops when each configured provider has five valid bodies or all
three rounds have finished. Missing provider credentials reduce available sources.

Only public HTML is read. Login-only, JavaScript-only, unsupported, sponsored,
short, and unparseable articles are skipped and counted. Search snippets are never
passed off as article bodies. Naver cafe search permission must be enabled in
API Hub. A search hit is not a guarantee that its full article is accessible.

Gemini receives up to ten distinct bodies, at most 3,000 characters each, with
source IDs and titles. Search results are ordered by publication recency when the
provider supports it. Publication dates are saved only when supplied by the API;
fetch dates are recorded separately. Fewer than ten usable sources can be analyzed; actual
counts and shortfall are reported. Insufficient sources during a provider/network
outage stay retryable rather than being treated as permanent content absence.

Reports include per-place source counts, body character counts, region availability,
collection shortfall, search errors, body filters and HTTP errors. Top-level
`exclusion_reasons`, `failure_reasons`, `body_filter_reasons`, `article_errors`, and
`search_errors` separate evidence/output problems from collection problems.

Budget implications: at most six Naver search calls and three Kakao search calls
per place (two Naver endpoints times three rounds). At 50 places and 20 scheduled
runs this is up to 6,000 Naver and 3,000 Kakao calls, excluding manual runs and other
clients. There is no shared daily search-call ledger. Body GETs are additional
requests to article hosts. Larger input increases Gemini token use and latency;
the existing time budget may defer remaining places. These are limits on work,
not a guarantee of ten usable sources or fifty completed places per run.

## Evidence and coverage policy

Pros and cons are optional arrays. Their absence does not invalidate a usable
three-line summary. Gemini selects deterministic `evidence_id` values from exact
source fragments; the server resolves IDs to quotes and source IDs. Unknown IDs,
short/missing quotes, unmatched quotes and ambiguous source references are logged
in `diagnostics.validation_failures`. Invalid generation gets one correction
request using exactly the same collected material. Optional points that remain
invalid are removed; the remaining summary is validated again. Provider/network
errors remain retryable and are never reclassified as absent evidence.

When visitor evidence is absent or cannot establish the place, the worker writes
an explicitly labeled `basic_info` result from the place name/address, with empty
pros/cons and a warning that review evidence is unavailable. This is a basic
coverage notice, not an invented AI visitor review. Reports and Telegram separate
`basic_info` from `review_analysis`. Unsupported/malformed main summaries may still
fail validation after correction; summary validation is not disabled.

## Periodic refresh and deployment

Apply `sql/ai_review_refresh.sql` to the batch database **before deploying this
worker**. It requires the existing `ai_review_two_pass_queue` schema. The SQL is a
transactional deployment patch; the local Supabase CLI was unavailable to generate
a migration. It can be incorporated into the deployment's migration system.

`enqueue_due_ai_reviews` runs at startup. Existing first-pass and second-pass work
has priority; afterward the oldest records last analyzed/attempted at least 180
days ago are queued again. Set the GitHub repository variable
`AI_REVIEW_REFRESH_DAYS` or CLI `--refresh-days` to change the interval. Source
revision/title/address changes enqueue work immediately. Active leases are not
reset. Failed periodic attempts respect the existing two-pass limit and wait for
the next refresh interval after exhaustion.

Successful result updates archive the previous ready result in the private,
RLS-enabled `ai_review_summary_history`. An unsuccessful refresh does not replace
a ready result, and a basic-info fallback cannot downgrade a ready review analysis.
The existing result remains visible with its original analysis time.

Customer-facing consumers can use `source_reviews.analyzed_at` (or legacy
`updated_at`) for the displayed analysis date, and `last_checked_at` for the most
recent attempt to check. `source_reviews.analysis_basis` distinguishes basic
information from visitor-review analysis. Publication dates in
`source_reviews.reviews[].published_at` are source dates, not verification dates.
The batch repo does not contain the customer UI; no customer display was changed.
