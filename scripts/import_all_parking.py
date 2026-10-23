"""Run both parking imports and persist their independent outcomes.

Existing deterministic upserts make reruns safe. A failed source does not prevent
collecting the other source's result; partial runs always return nonzero.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run_imports(dry_run=False, output=None, runner=subprocess.run):
    output = Path(output) if output else ROOT / 'artifacts/parking-integrated'
    output.mkdir(parents=True, exist_ok=True)
    report = {'checked_at': datetime.now(timezone.utc).isoformat(),
              'dry_run': dry_run, 'sources': {}, 'status': 'running'}
    for name, script in [('STANDARD', 'import_parking_lots.py'), ('LX', 'import_lx_parking.py')]:
        command = [sys.executable, str(ROOT / 'scripts' / script)]
        if dry_run:
            command.append('--dry-run')
        if name == 'LX':
            command.extend(['--output', str(output / 'lx')])
        try:
            result = runner(command, cwd=ROOT, capture_output=True, text=True,
                            encoding='utf-8', errors='replace')
            summary = None
            # Only persist structured summaries; raw network logs can contain secrets.
            for line in reversed(result.stdout.splitlines()):
                try:
                    value = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(value, dict):
                    summary = value
                    break
            report['sources'][name] = {'exit_code': result.returncode,
                                       'summary': summary,
                                       'status': 'complete' if result.returncode == 0 else 'needs_review'}
        except OSError as error:
            report['sources'][name] = {'status': 'failed', 'error_type': type(error).__name__}
        report['status'] = 'running'
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    complete = all(source['status'] == 'complete' for source in report['sources'].values())
    report['status'] = 'complete' if complete else 'needs_review'
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='No database writes; LX may geocode and update local cache')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/parking-integrated')
    args = parser.parse_args(argv)
    report = run_imports(args.dry_run, args.output)
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
