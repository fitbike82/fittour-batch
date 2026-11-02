# FitTour Batch

Python and TypeScript workspace for FitTour's tourism collection, AI review summaries and nationwide parking imports.

This repository was extracted from `fitbike82/local-trip-ai` without changing or deleting the original project. It uses the existing Supabase database and API providers; the TourAPI TypeScript pipeline requires the additive migrations documented below.

## Runtime and commands

Use Python 3.12 for legacy jobs and Node.js 22+ for the scheduled TourAPI pipeline.

```sh
python -m venv .venv
# Activate the environment for your shell, then:
python -m pip install -r requirements.txt
cp .env.example .env
# Fill server-side credentials locally. Never commit the populated file.
python -m unittest -v
python scripts/batch_ai_summary_cron.py --limit 1
python scripts/sync_places_universe.py --area all --mode sync --batch-size 1000
python scripts/sync_tour_enrichment_full.py --limit 50000 --endpoint-daily-limit 100000 --with-endpoint-daily-limit 1000 --workers 4 --max-seconds 2700 --delay 0.1
python scripts/import_all_parking.py --dry-run
```

All original Python implementations live in `scripts/`. The root Python files are compatibility entry points so existing imports, mocks and commands continue working. The existing Python test suite is retained; the LX CSV case uses a synthetic CP949 fixture so CI does not require publishing the raw source dataset. Parking importers accept operator-supplied CSV files with --file. For the integrated importer, place the public standard parking CSV and LX CSV at the existing docs/ default filenames shown in each script. These source datasets are not published in this code repository. Parking dry runs may geocode addresses and use Kakao quota.

## GitHub Actions

| Workflow | Purpose | Trigger |
| --- | --- | --- |
| Validate batch integrations | Read-only database, TourAPI, Kakao and Gemini authentication checks | manual |
| Batch CI | Install the complete runtime, dependency check, compile, Python tests, TypeScript checks and Node tests | push / pull request / manual |
| Daily AI review summary batch | Review queue with existing transactional claims, evidence validation and recovery | manual, optional 20 hourly slots daily |
| Daily master catalog sync | Modified-place upsert with checkpoint recovery | manual, UTC 16:00 daily (01:00 KST) |
| Daily Tour Enrichment | Three detail relays with shared daily quota and live DB reports | manual, UTC 17:00/19:00/21:00 (02:00/04:00/06:00 KST) |

Every batch workflow includes `workflow_dispatch`. In the GitHub Actions tab, select **Daily AI review summary batch**, choose **Run workflow**, and use limit **1** for the first live test. Manual AI batches default to 1 place; scheduled batches use 50. Reports are uploaded as Actions artifacts. A separate report check fails the run on hidden per-place errors, lease-release errors, or quota deferral instead of claiming success solely from process exit code.

### No-interruption migration

The original extraction gated schedules with `BATCH_SCHEDULE_ENABLED`. TASK-032 replaces
the master and Tour workflows with the approved 01:00 master refresh and three relays;
these two workflows are scheduled directly after deployment to the default branch.
The AI summary workflow retains its existing gate. The new Tour workflows share one
concurrency group and a transactional Supabase quota ledger. Other repositories and
manual legacy Python runs must be accounted for separately because they do not reserve
calls in this ledger.

The Tour pipeline uses DB claims and 30-minute recoverable leases. Legacy Python page
journals remain available for manual operation. Apply both additive migrations before
deploying the workflows. See [TASK-032 rollout and schedule](docs/task-032.md).

### Required repository secrets

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `TOUR_API_DECODING_KEY`
- `KAKAO_REST_API_KEY`
- `GEMINI_API_KEY`
- `SUPABASE_KEY`: optional legacy fallback
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`: optional notifications; enabled only with `BATCH_NOTIFICATIONS_ENABLED=true`

No secrets are stored in code, datasets, sample environment files, logs committed to Git, or artifacts committed to Git. `.env*`, caches, virtual environments, provider reports, SQLite journals and logs are ignored. GitHub secret names can be checked without exposing their values.

See `docs/migration-report.md` for deployment evidence and known limits.

### Gemini project key pool

Set GEMINI_API_KEY_01, GEMINI_API_KEY_02 and GEMINI_API_KEY_03 as repository Secrets for independent Google projects. If none are configured, GEMINI_API_KEY remains the legacy fallback. Scheduled batches distribute their starting keys 7/7/6 across the 20 daily runs; quota errors immediately try the next available key.

Daily exhaustion is cached across serialized Actions runs using only a SHA-256 key fingerprint and the Pacific quota date. Transient RPM errors are not persisted for the day. All-key quota exhaustion releases queued work and ends successfully; actual processing/release errors still fail. Cache outages fall back to per-process circuits. Use GEMINI_BATCH_SLOT=1..20 for deterministic local selection and GEMINI_KEY_POOL_STATE for an optional local state file under an ignored artifacts directory. Never commit populated keys or state files.


### Approved Korean TourAPI production quota

The scheduled TypeScript pipeline shares a conservative 100000-call KST budget across
catalog/common/intro/course requests and retries. It uses 1000-row catalog pages and
caps actual HTTP concurrency at ten. Courses include all detailInfo2 pages. The old
manual Python collector retains its separate per-operation budgets and page journal;
it is no longer invoked by the daily Tour workflow.

## TASK-030 / TASK-031 TypeScript batches

Node.js 22+ one-off nationwide catalog and concurrent detail collection:

```sh
npm ci
npm run sync:master -- --env-file /path/to/server.env
npm run batch-tour-enrichment -- --env-file /path/to/server.env
```

Apply the additive migration first. See [installation, schema mapping, quotas,
resume behavior and validation](docs/task-030-031.md). The full master CLI is one-off.
The daily incremental pipeline schedules master and detail updates together. The commands share a maximum
of 100000 TourAPI calls per KST day; existing Python jobs have separate accounting and must not overlap a full
backfill without accounting for their usage.


## TASK-032 daily master and detail relay

```sh
npm run sync:daily-master -- --env-file /path/to/server.env
npm run batch-tour-enrichment -- --categories 39 --limit 100 --max-seconds 5400
npm run batch:report -- --env-file /path/to/server.env
```

See [rollout, cumulative relay budgets, live report definitions and validation](docs/task-032.md).

## Daily incremental pipeline

```sh
npm run sync:daily-tour -- --env-file /path/to/server.env --max-seconds 5400
```

Runs master changes and inactive-state updates before enrichment using the same
API client and quota ledger. Each run caps actual master changes and detail claims
at 10,000 per phase; smaller `--limit` values are supported. The scheduled run is
once daily at 01:00 KST. Bulk relays are manual. See
[safety limits, checkpoint recovery and deployment](docs/daily-tour-pipeline.md).

## 백년가게 이력과 공통 카테고리

```sh
npm run import:century
# 확실한 후보를 DB에 반영
npm run import:century -- --apply
```

CSV 매칭과 Gemini는 `src/lib/taxonomy.ts`를 원본으로 생성한 공통 정의를 공유한다.
수정 후 `npm run taxonomy:sync`로 Python용 JSON을 갱신하고
`npm run taxonomy:check`로 일치 여부를 검증한다.
2022년 CSV 수록 이력은 현재 인증 여부와 구분해 저장한다. AI 저장 함수를 포함한
[SQL 적용 순서와 매칭 기준](docs/century-store-taxonomy.md)을 먼저 확인한다.

## 잔여 백년가게 AI 후보 매칭

```sh
python scripts/century_ai_matching.py --prepare
python scripts/century_ai_matching.py --limit 50 --slot manual
```

PM 비외식 사전으로 먼저 거르고, 동일 시군구의 상위 3개 후보만 심사한다.
HTTP 재시도에도 4.5초 간격·15 RPM을 적용하며 회차당 50건/일일 최대 250건이다.
[큐 보존, 새벽 스케줄 및 모델 설정](docs/century-ai-matching.md)을 참고한다.
