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
source IDs and titles. Existing exact-quote validation and quality requirements
remain in effect. Fewer than ten usable sources can still be analyzed; actual
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
