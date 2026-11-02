import unittest
from unittest.mock import Mock

from postgrest.exceptions import APIError
from batch_ai_summary_cron import refresh_queue


class RefreshStartupTests(unittest.TestCase):
    def test_timeout_retries_smaller_batches_and_recovers(self):
        db, sleep = Mock(), Mock()
        db.rpc.return_value.execute.side_effect = [
            APIError({'code': '57014', 'message': 'private SQL'}), Mock(data=4)]
        result = refresh_queue(db, 50, 180, sleep)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(result['errors'], ['database_57014'])
        self.assertEqual([c.args[1]['batch_limit'] for c in db.rpc.call_args_list], [50, 25])
        sleep.assert_called_once_with(1)

    def test_exhaustion_defers_maintenance_without_aborting_worker(self):
        db, sleep = Mock(), Mock()
        db.rpc.return_value.execute.side_effect = APIError({'code': '57014', 'message': 'private SQL'})
        result = refresh_queue(db, 1, 180, sleep)
        self.assertEqual(result['status'], 'deferred')
        self.assertEqual(result['attempts'], 3)
        self.assertNotIn('private', str(result))
        self.assertTrue(all(c.args[1]['batch_limit'] == 1 for c in db.rpc.call_args_list))

    def test_permissions_and_ambiguous_transport_errors_are_not_retried(self):
        for error in [APIError({'code': '42501', 'message': 'private'}), OSError('private')]:
            db, sleep = Mock(), Mock()
            db.rpc.return_value.execute.side_effect = error
            with self.assertRaises(type(error)):
                refresh_queue(db, 50, 180, sleep)
            self.assertEqual(db.rpc.call_count, 1)
            sleep.assert_not_called()

    def test_busy_maintenance_is_visible(self):
        db = Mock()
        db.rpc.return_value.execute.return_value.data = -1
        self.assertEqual(refresh_queue(db, 50, 180)['status'], 'busy')

    def test_main_continues_after_refresh_exhaustion_and_reports_empty_queue(self):
        import json
        import os
        import tempfile
        from pathlib import Path
        from contextlib import ExitStack
        from unittest.mock import patch
        from batch_ai_summary_cron import main
        for selected in (0, 1):
            db = Mock()
            db.rpc.return_value.execute.side_effect = APIError({'code': '57014', 'message': 'private'})
            with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                report = Path(directory) / 'report.json'
                stack.enter_context(patch.dict(os.environ, {
                    'SUPABASE_URL': 'https://example.supabase.co', 'SUPABASE_SERVICE_ROLE_KEY': 'private'}, clear=True))
                stack.enter_context(patch('sys.argv', ['batch', '--report', str(report)]))
                for name, value in [('load_dotenv', None), ('has_search_credentials', True),
                                    ('discover_keys', ['key']), ('create_client', db), ('notify', True),
                                    ('Gemini', Mock()), ('budget_sleep', None)]:
                    stack.enter_context(patch('batch_ai_summary_cron.' + name, return_value=value))
                run = stack.enter_context(patch('batch_ai_summary_cron.run', return_value={
                    'selected': selected, 'processed': selected, 'success': selected, 'excluded': 0,
                    'errors': 0, 'deferred': 0, 'pass_number': 1, 'places_started': selected,
                    'basic_info': 0, 'review_analysis': selected, 'elapsed_seconds': 1, 'quota_deferred': 0}))
                self.assertEqual(main(), 0)
                run.assert_called_once()
                data = json.loads(report.read_text(encoding='utf-8'))
                self.assertEqual(data['refresh_queue']['status'], 'deferred')
                self.assertEqual(data['deferred'], 1 if not selected else 0)
                self.assertFalse(data.get('fatal', False))
