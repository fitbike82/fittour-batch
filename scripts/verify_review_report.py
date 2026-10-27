"""Require a report and no hidden per-place failures during first deployment."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--allow-quota-stop', action='store_true')
    args = parser.parse_args()
    path = Path('artifacts/ai-review-batch/report.json')
    if not path.is_file():
        raise SystemExit('Review batch did not produce its report')
    report = json.loads(path.read_text(encoding='utf-8'))
    print(json.dumps({key: report.get(key) for key in (
        'selected', 'processed', 'success', 'excluded', 'errors', 'release_errors',
        'quota_deferred', 'quota_stopped', 'stop_reason')}, ensure_ascii=True))
    quota_only = (args.allow_quota_stop and report.get('quota_stopped', False)
                  and report.get('stop_reason') == 'daily_quota_exceeded'
                  and not report.get('errors', 0) and not report.get('release_errors', 0))
    if quota_only:
        return
    if report.get('deferred', 0) or report.get('errors', 0) or report.get('release_errors', 0) or report.get('quota_deferred', 0) or report.get('quota_stopped', False):
        raise SystemExit('Batch completed with deferred quota or per-place errors; inspect the report')


if __name__ == '__main__':
    main()
