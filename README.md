# FitTour Batch

Standalone Python workspace for FitTour's unattended tourism collection, AI review summaries and nationwide parking imports.

This repository was extracted from `fitbike82/local-trip-ai` without changing or deleting the original project. It uses the existing Supabase database and API providers; no frontend, new database, or schema deployment is required.

## Runtime and commands

Use Python 3.12 (the GitHub Actions runtime).

```sh
python -m venv .venv
# Activate the environment for your shell, then:
python -m pip install -r requirements.txt
cp .env.example .env
# Fill server-side credentials locally. Never commit the populated file.
python -m unittest -v
python scripts/batch_ai_summary_cron.py --limit 1
python scripts/sync_places_universe.py --area all --mode sync --batch-size 500
python scripts/sync_tour_enrichment_full.py --limit 10000 --endpoint-daily-limit 1000 --max-seconds 2700 --delay 0.2
python scripts/import_all_parking.py --dry-run
```

All original Python implementations live in `scripts/`. The root Python files are compatibility entry points so existing imports, mocks and commands continue working. The original 120 tests are included unchanged. Parking importers accept operator-supplied CSV files with --file. For the integrated importer, place the public standard parking CSV and LX CSV at the existing docs/ default filenames shown in each script. These source datasets are not published in this code repository. Parking dry runs may geocode addresses and use Kakao quota.

## GitHub Actions

| Workflow | Purpose | Trigger |
| --- | --- | --- |
| Validate batch integrations | Read-only database, TourAPI, Kakao and Gemini authentication checks | manual |
| Batch CI | Install the complete runtime, dependency check, compile and all 120 tests | push / pull request / manual |
| Daily AI review summary batch | Review queue with existing transactional claims, evidence validation and recovery | manual, optional 20 hourly slots daily |
| Daily places universe sync | TourAPI place catalog sync | manual, optional UTC 15:30 daily |
| Daily Tour enrichment | Resumable detail/image collection and independent operation budgets | manual, optional UTC 17:00/19:00/21:00 |

Every batch workflow includes `workflow_dispatch`. In the GitHub Actions tab, select **Daily AI review summary batch**, choose **Run workflow**, and use limit **1** for the first live test. Manual AI batches default to 1 place; scheduled batches use 50. Reports are uploaded as Actions artifacts. A separate report check fails the run on hidden per-place errors, lease-release errors, or quota deferral instead of claiming success solely from process exit code.

### No-interruption migration

The original repository continues running unchanged. Scheduled jobs in this new repository are deliberately gated by repository variable `BATCH_SCHEDULE_ENABLED=true`. An unset/false variable leaves manual runs enabled and scheduled jobs skipped. Enable this only when the old schedule has been retired in a separately approved cutover. GitHub concurrency groups do not coordinate across repositories, and TourAPI journal caches are repository-scoped; enabling both schedules would duplicate collection and quota use.

The original AI queue's database claim/release protocol is retained, so a bounded manual test does not bypass work leasing. Tour enrichment retains its existing repository-local resumable journal and cache. Existing production database schemas are prerequisites. This extraction makes no database schema changes and does not publish or automatically reapply historical SQL.

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
