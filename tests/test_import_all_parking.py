import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.import_all_parking import run_imports


class IntegratedParkingTests(unittest.TestCase):
    def test_both_sources_receive_dry_run_and_preserve_counts(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, 'progress\n{"upserted": 171}\n', '')
        with tempfile.TemporaryDirectory() as directory:
            report = run_imports(True, directory, runner)
            self.assertEqual(report['status'], 'complete')
            self.assertEqual(len(calls), 2)
            self.assertTrue(all('--dry-run' in call for call in calls))
            self.assertEqual(report['sources']['LX']['summary']['upserted'], 171)
            self.assertEqual(json.loads((Path(directory) / 'report.json').read_text())['status'], 'complete')

    def test_partial_import_does_not_skip_second_source_or_claim_success(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 1 if len(calls) == 1 else 0, '', 'secret raw error')
        with tempfile.TemporaryDirectory() as directory:
            report = run_imports(False, directory, runner)
            self.assertEqual(len(calls), 2)
            self.assertEqual(report['status'], 'needs_review')
            self.assertNotIn('secret', (Path(directory) / 'report.json').read_text())
            self.assertTrue(all('--dry-run' not in call for call in calls))

    def test_launch_failure_still_records_both_sources(self):
        def runner(*args, **kwargs):
            raise OSError('private environment contents')
        with tempfile.TemporaryDirectory() as directory:
            report = run_imports(True, directory, runner)
            self.assertEqual(report['status'], 'needs_review')
            self.assertEqual(len(report['sources']), 2)
            self.assertNotIn('private', json.dumps(report))
