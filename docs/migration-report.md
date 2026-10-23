# TASK-025-A deployment report

Completed: 2026-10-23 (Asia/Seoul).

Repository: https://github.com/fitbike82/fittour-batch
Local workspace: C:/Users/Lotte/fittour-batch
Application/workflow code revision: 2f9b8e6.

## Delivered workspace

- All 11 original Python batch implementations are under scripts/: AI review generation, legacy enrichment, place catalog synchronization, resumable TourAPI enrichment and standard/LX parking importers.
- Root compatibility entry points preserve existing imports, logger names, test patches and command names.
- Complete pinned Python dependencies in requirements.txt, with the original narrower requirements files retained for compatibility. GitHub Actions uses Python 3.12.
- All 120 regression cases are retained. One LX CSV case now constructs a synthetic CP949 fixture instead of requiring the original raw dataset. It still checks decoding, 179 records, deterministic IDs, coordinates, fee handling and source-date metadata. No test is skipped.
- .gitignore excludes credentials, environments, Python caches, runtime reports, journals and logs. .env.example contains empty server-side variable names only.
- README.md documents setup, CLI entry points, required Secrets, CSV inputs, manual runs and the later schedule cutover.
- Public Git payload: 49 explicitly selected batch, test, configuration and newly authored documentation files. Original raw datasets, historical SQL and copied operational/provider documents remain local-only and ignored. Parking importers accept operator-supplied CSV inputs.

## Published workflows

| File | Purpose |
| --- | --- |
| .github/workflows/ci.yml | Dependency installation/check, compilation and all 120 tests |
| .github/workflows/daily_ai_summary_batch.yml | Scheduled/manual AI review batch; manual default limit 1 |
| .github/workflows/daily_places_sync.yml | Scheduled/manual TourAPI catalog synchronization |
| .github/workflows/daily_tour_batch.yml | Scheduled/manual resumable TourAPI enrichment |
| .github/workflows/validate_integrations.yml | Manual read-only database and provider integration checks |

All three batch workflows retain workflow_dispatch. AI cron is 0 0-16,21-23 * * * as requested. All five workflows are registered as active in GitHub.

## Remote verification

| Check | Result | Evidence |
| --- | --- | --- |
| Latest Linux CI | Passed; install, pip check, compileall and all 120 tests | https://github.com/fitbike82/fittour-batch/actions/runs/39927768126 |
| Latest integration validation | Passed; five database table queries, enrichment-schema preflight, TourAPI, Kakao and Gemini authentication | https://github.com/fitbike82/fittour-batch/actions/runs/39927774788 |
| Bounded live AI batch | Passed; selected/processed 1, excluded 1, errors/release errors/deferred/quota deferred all 0 | https://github.com/fitbike82/fittour-batch/actions/runs/39927551542 |
| Actual persistence | Remote report says committed; read-only database lookup independently confirms quality_status=insufficient | Local artifacts/persistence-verification.json |
| Local regression suite | All 120 passed after independent-fixture adaptation | Local artifacts/local-tests.log |
| Original project unchanged | All 180 tracked-file hashes and initial Git status match the pre-copy snapshot | Local artifacts/source-baseline.json and check_migration.py |
| Credential scan | Passed; no actual source environment secrets or source Git credential in public files | Local publication checks |

The live AI case invoked Gemini once and was correctly excluded for insufficient evidence; it did not create an approved ready summary. The exclusion outcome was persisted successfully. No claim of a ready-summary success is made from the workflow exit code alone.

Initial cloud validation identified a raw-CSV test dependency and a TourAPI credential mismatch. The new repository's fixture was made independent, and only its TOUR_API_DECODING_KEY Secret was updated from a locally verified working credential. The original environment files and original repository Secrets were not changed. Values were neither printed nor committed.

## No-interruption state and cutover

- local-trip-ai files and workflows were not edited, deleted, disabled or moved. Its pre-existing .env.example modification remains unchanged.
- The 11 original script copies remain byte-identical locally. Fourteen test/discovery files remain byte-identical; only the new repository's LX fixture case was adapted.
- No schema migration or bulk catalog/parking import was executed. TourAPI/catalog/enrichment paths were validated through CI and read-only provider/schema checks rather than starting a second full ingestion run.
- The original AI database claim/release protocol was retained for the bounded live test.
- New scheduled jobs require repository variable BATCH_SCHEDULE_ENABLED=true. It remains unset, so manual runs work and new scheduled processing is skipped while the original continues operating. GitHub concurrency groups and journal caches are repository-scoped; schedule overlap is deliberately avoided.
- Telegram notifications remain opt-in via BATCH_NOTIFICATIONS_ENABLED=true. No Telegram notification was sent during these validation runs.
- Retiring the original schedules and enabling the new schedules is a separate cutover, not performed in TASK-025-A.

## Local validation evidence (ignored by Git)

- artifacts/local-tests.log
- artifacts/source-baseline.json
- artifacts/remote-ai-validation/
- artifacts/remote-preflight-validation/
- artifacts/persistence-verification.json

The repository is published, manually runnable and remotely verified as described above. The original deployment remains intact.
