import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import sync_tour_enrichment_full as batch
from test_tour_enrichment_full import Http, Response, payload


class UnlimitedBatchTests(unittest.TestCase):
    def test_provider_quota_reports_both_services_and_waits_successfully(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Mock()
            db.rpc.return_value = True
            db.completed.return_value = set()
            db.collected_ids.return_value = set()
            db.targets.return_value = [dict(content_id='1', content_type_id='12')]
            client = Mock()
            client.collect.side_effect = batch.QuotaStop('22', '<provider>quota exhausted</provider>')
            report, context, output = {}, {}, io.StringIO()
            with patch.object(batch, 'Database', return_value=db), patch.object(batch, 'TourClient', return_value=client), \
                    patch.object(batch, 'load_dotenv'), patch.dict(os.environ, dict(TOUR_API_KEY='key', SUPABASE_URL='url', SUPABASE_KEY='key')), \
                    contextlib.redirect_stdout(output):
                try:
                    code = batch._run_batch(['--checkpoint', str(Path(directory) / 'cache.db')], report, context)
                    self.assertEqual(code, 0)
                    self.assertTrue(report['quota_stopped'])
                    self.assertEqual(report['processed_count'], 0)
                    for response in report['quota_responses']:
                        self.assertIn(response['body'], output.getvalue())
                    self.assertEqual(len(report['quota_responses']), 10)
                    self.assertEqual(len(report['blocked_endpoints']), 10)
                    self.assertEqual(client.collect.call_count, 10)
                finally:
                    context['journal'].db.close()

    def test_exhausted_service_does_not_block_other_service_or_mask_failures(self):
        for fail_storage in (False, True):
            with self.subTest(fail_storage=fail_storage), tempfile.TemporaryDirectory() as directory:
                db = Mock()
                db.rpc.return_value = True
                db.completed.return_value = set()
                db.collected_ids.return_value = set()
                db.targets.return_value = [dict(content_id=str(i), content_type_id='12') for i in range(3)]
                if fail_storage:
                    db.rpc.side_effect = lambda name, data: True if name == 'tour_enrichment_preflight' else (_ for _ in ()).throw(RuntimeError('storage down'))
                client = Mock()
                client.collect.side_effect = lambda service, *args: (_ for _ in ()).throw(batch.QuotaStop('22', 'daily quota')) if service == 'KorService2' else ([payload([])], [])
                report, context = {}, {}
                with patch.object(batch, 'Database', return_value=db), patch.object(batch, 'TourClient', return_value=client), \
                        patch.object(batch, 'load_dotenv'), patch.dict(os.environ, dict(TOUR_API_KEY='key', SUPABASE_URL='url', SUPABASE_KEY='key')), \
                        contextlib.redirect_stdout(io.StringIO()):
                    try:
                        code = batch._run_batch(['--checkpoint', str(Path(directory) / 'cache.db')], report, context)
                        self.assertEqual(code, int(fail_storage))
                        self.assertEqual(report['places_attempted'], 3)
                        self.assertEqual(report['processed_count'], 0)
                        self.assertEqual(sum(call.args[0] == 'KorService2' for call in client.collect.call_args_list), 5)
                        self.assertEqual(sum(call.args[0] == 'KorWithService2' for call in client.collect.call_args_list), 15)
                        self.assertEqual(report['endpoints_saved'], 0 if fail_storage else 15)
                    finally:
                        context['journal'].db.close()

    def test_300_unfinished_after_completed_prefix_without_quota_rpc(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Mock()
            db.rpc.return_value = True
            db.targets.return_value = [dict(content_id=str(i), content_type_id='12', modified_time='v1') for i in range(310, 610)]
            db.completed.return_value = {(str(i), service, endpoint, 'v1') for i in range(310)
                                         for service, endpoints in batch.SERVICES.items() for endpoint in endpoints}
            db.collected_ids.return_value = {str(i) for i in range(310)}
            client = Mock()
            client.collect.return_value = ([payload([])], [])
            report, context = {}, {}
            with patch.object(batch, 'Database', return_value=db), patch.object(batch, 'TourClient', return_value=client), \
                    patch.object(batch, 'load_dotenv'), patch.dict(os.environ, dict(TOUR_API_KEY='key', SUPABASE_URL='url', SUPABASE_KEY='key')), \
                    contextlib.redirect_stdout(io.StringIO()):
                try:
                    code = batch._run_batch(['--checkpoint', str(Path(directory) / 'cache.db'),
                                             '--daily-limit', '999999999', '--service-daily-limit', '1'], report, context)
                    self.assertEqual(code, 0)
                    self.assertEqual(report['processed_count'], 300)
                    self.assertEqual(report['places_attempted'], 300)
                    ids = {call.args[2]['content_id'] for call in client.collect.call_args_list}
                    self.assertEqual(ids, {str(i) for i in range(310, 610)})
                    self.assertFalse(any(call.args[0] == 'reserve_tour_enrichment_call' for call in db.rpc.call_args_list))
                finally:
                    if 'journal' in context:
                        context['journal'].db.close()

    def test_real_provider_quota_bodies_preserved_for_json_xml_http(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = batch.Journal(str(Path(directory) / 'cache.db'), 'key', 0, 0)
            try:
                xml = Response({})
                xml.content = b'<error><returnReasonCode>22</returnReasonCode><message>quota</message></error>'
                for response in (Response(payload([], code='22')), xml, Response({'error': 'quota'}, 429)):
                    db = Mock()
                    db.rpc.side_effect = AssertionError('quota RPC must never run')
                    client = batch.TourClient('key', journal, db=db, delay=0)
                    client.http = Http([response])
                    with self.assertRaises(batch.QuotaStop) as caught:
                        client.collect('KorService2', 'detailInfo2', dict(content_id='1', content_type_id='12'))
                    self.assertEqual(caught.exception.response_body, response.content.decode('utf-8-sig'))
                    db.rpc.assert_not_called()
            finally:
                journal.db.close()

    @patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'token', 'TELEGRAM_CHAT_ID': 'chat'})
    @patch.object(batch.requests, 'post')
    def test_long_response_body_sent_without_truncation(self, post):
        post.return_value = Mock(status_code=200, json=lambda: {'ok': True})
        report = dict(processed_count=2, quota_stopped=True, api_status='22', api_response_body='차단\n' * 4000)
        self.assertTrue(batch.send_telegram_report(report))
        delivered = ''.join(call.kwargs['json']['text'] for call in post.call_args_list)
        self.assertEqual(delivered, batch.telegram_report(report) + '\n' + report['api_response_body'])
