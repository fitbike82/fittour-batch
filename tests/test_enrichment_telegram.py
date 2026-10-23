import contextlib
import io
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import requests
from scripts import sync_tour_enrichment_full as batch


class TelegramReportTests(unittest.TestCase):
    def setUp(self):
        self.report = dict(processed_count=12, total_enriched_count=4186,
                           day_calls={'KorService2': 100, 'KorWithService2': 80},
                           images_count=30, barrier_count=4, status='정상 완료 (다음 배치 대기)')

    @patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'test-token', 'TELEGRAM_CHAT_ID': 'test-chat'})
    @patch.object(batch.requests, 'post')
    def test_send_message_payload_and_kst(self, post):
        post.return_value = Mock(status_code=200)
        post.return_value.json.return_value = {'ok': True}
        self.assertTrue(batch.send_telegram_report(self.report))
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], 'https://api.telegram.org/bottest-token/sendMessage')
        body = post.call_args.kwargs['json']
        self.assertEqual(body['chat_id'], 'test-chat')
        for text in ('4186 / 8,372', '(50.0%)', '전체 완료: 12곳', 'API'):
            self.assertIn(text, body['text'])
        self.assertNotIn('parse_mode', body)

    @patch.dict(os.environ, {}, clear=True)
    @patch.object(batch.requests, 'post')
    def test_missing_credentials_skip_network(self, post):
        self.assertFalse(batch.send_telegram_report(self.report))
        post.assert_not_called()

    @patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'test-token', 'TELEGRAM_CHAT_ID': 'test-chat'})
    @patch.object(batch.requests, 'post')
    def test_network_and_api_failure_never_leak_token_or_raise(self, post):
        for result in (requests.Timeout('https://api.telegram.org/bottest-token/sendMessage'), Mock(status_code=429), Mock(status_code=200, json=lambda: {'ok': False})):
            post.reset_mock()
            post.side_effect = result if isinstance(result, Exception) else None
            post.return_value = result
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                self.assertFalse(batch.send_telegram_report(self.report))
            self.assertNotIn('test-token', output.getvalue())
            post.assert_called_once()

    def test_complete_requires_all_endpoints_at_the_same_revision(self):
        all_rows = {('a', service, endpoint, 'v1') for service, endpoints in batch.SERVICES.items() for endpoint in endpoints}
        self.assertEqual(batch.enriched_count(all_rows), 1)
        missing = next(iter(all_rows))
        self.assertEqual(batch.enriched_count(all_rows - {missing}), 0)
        mixed = all_rows - {missing} | {(missing[0], missing[1], missing[2], 'v2')}
        self.assertEqual(batch.enriched_count(mixed), 0)

    def test_daily_processing_is_durable_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'journal.sqlite3')
            journal = batch.Journal(path, 'key')
            journal.processed('a'); journal.processed('a'); journal.processed('b')
            journal.reserve('KorService2')
            journal.db.close()
            resumed = batch.Journal(path, 'key')
            self.assertEqual(resumed.day_processed(), 2)
            self.assertEqual(resumed.day_calls(), {'KorService2': 1, 'KorWithService2': 0})
            resumed.db.close()

    @patch.object(batch, 'send_telegram_report')
    def test_normal_partial_quota_and_fatal_exit_report_once(self, send):
        with tempfile.TemporaryDirectory() as directory, patch.object(batch, 'Path', side_effect=lambda value: Path(directory) / value):
            for code, fields, status in ((0, {}, '정상 완료'), (1, {}, '일부 실패'), (0, {'quota_stopped': True}, '일일 한도')):
                send.reset_mock()
                def run(argv, report, context):
                    report.update(fields)
                    return code
                with patch.object(batch, '_run_batch', side_effect=run):
                    self.assertEqual(batch.main([]), code)
                send.assert_called_once()
                self.assertIn(status, send.call_args.args[0]['status'])
            send.reset_mock()
            with patch.object(batch, '_run_batch', side_effect=RuntimeError('secret-value')):
                with self.assertRaises(RuntimeError):
                    batch.main([])
            send.assert_called_once()
            self.assertIn('예외 종료', send.call_args.args[0]['status'])
            self.assertNotIn('secret-value', str(send.call_args.args[0]))


if __name__ == '__main__':
    unittest.main()
