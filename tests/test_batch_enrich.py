import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from batch_enrich_cron import Batch, BudgetExhausted, load_batch_config, main, send_telegram_alert


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.http = Mock()
        self.batch = Batch(self.db, 'private-key', 'https://example.supabase.co', self.http)
        self.db.rpc.return_value.execute.return_value.data = True

    def test_exhausted_budget_never_calls_api(self):
        self.db.rpc.return_value.execute.return_value.data = False
        with self.assertRaises(BudgetExhausted):
            self.batch.detail('detailCommon2', {'content_id': '123'})
        self.http.get.assert_not_called()

    def test_429_stops_without_retry(self):
        self.http.get.return_value.status_code = 429
        with self.assertRaises(BudgetExhausted):
            self.batch.detail('detailCommon2', {'content_id': '123'})
        self.assertEqual(self.http.get.call_count, 1)

    def test_head_206_uses_content_range_total(self):
        import httpx
        from postgrest import SyncPostgrestClient
        client = SyncPostgrestClient('https://example.invalid')
        requests = []
        counts = iter([37, 1773])
        def respond(http_client, request, **kwargs):
            requests.append(request)
            return httpx.Response(206, request=request, headers={
                'Content-Range': f'0-24/{next(counts)}', 'Content-Type': 'application/json'})
        self.batch.db = SimpleNamespace(table=client.from_)
        with patch.object(httpx.Client, 'send', respond):
            self.assertEqual(self.batch.completion_counts(), (37, 1773))
        self.assertEqual([request.method for request in requests], ['HEAD', 'HEAD'])
        self.assertTrue(all('count=exact' in request.headers['prefer'] for request in requests))
        client.session.close()

    def test_partial_failure_then_quota_stop_is_successful(self):
        self.batch.preflight = Mock()
        query = self.db.table.return_value.select.return_value.eq.return_value.eq.return_value.or_.return_value.order.return_value.order.return_value.limit.return_value
        query.execute.return_value.data = [{'content_id': str(i)} for i in range(3)]
        for failures, expected_processed, stopped in (
                ([RuntimeError('bad place'), None, None], 2, False),
                ([RuntimeError('bad place'), BudgetExhausted(), None], 0, True)):
            with self.subTest(stopped=stopped):
                self.batch.processed = 0
                self.batch.errors = []
                self.batch.quota_stopped = False
                self.batch.enrich = Mock(side_effect=failures)
                with patch('batch_enrich_cron.traceback.print_exc'):
                    self.assertEqual(self.batch.run(3), 0)
                self.assertEqual(self.batch.processed, expected_processed)
                self.assertEqual(self.batch.quota_stopped, stopped)
                self.assertEqual(len(self.batch.errors), 1)

    def test_upload_and_readback_webp(self):
        raw = io.BytesIO()
        Image.new('RGB', (1600, 800)).save(raw, 'PNG')
        response = Mock(status_code=200, content=raw.getvalue())
        self.http.get.return_value = response
        bucket = self.db.storage.from_.return_value
        bucket.download.side_effect = lambda path: bucket.upload.call_args.args[1]
        bucket.get_public_url.return_value = 'https://example.supabase.co/storage/v1/object/public/places/123.webp'
        self.batch.image({'content_id': '123', 'first_image': 'https://source/image'}, {})
        with Image.open(io.BytesIO(bucket.upload.call_args.args[1])) as image:
            self.assertEqual((image.format, image.size), ('WEBP', (800, 400)))

    def test_failed_storage_does_not_mark_place_complete(self):
        self.batch.detail = Mock(side_effect=[{'overview': 'text'}, {'usetime': '9-5'}])
        self.batch.image = Mock(side_effect=RuntimeError('storage failure'))
        with self.assertRaises(RuntimeError):
            self.batch.enrich({'content_id': '123', 'content_type_id': '12'})
        self.db.table.assert_called_once_with('place_details')

    def test_no_source_is_not_image_success(self):
        self.assertIsNone(self.batch.image({'content_id': '123'}, {}))
        self.http.get.assert_not_called()

    def test_invalid_limit_rejected_before_storage(self):
        with self.assertRaises(ValueError):
            self.batch.run(2000)
        self.db.storage.get_bucket.assert_not_called()


class TelegramTests(unittest.TestCase):
    def test_missing_secrets_skip_network(self):
        with patch.dict('os.environ', {}, clear=True), patch('batch_enrich_cron.requests.post') as post:
            self.assertFalse(send_telegram_alert('start'))
            post.assert_not_called()

    def test_successful_request(self):
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'test-token', 'TELEGRAM_CHAT_ID': '123'}), patch('batch_enrich_cron.requests.post') as post:
            post.return_value.json.return_value = {'ok': True}
            self.assertTrue(send_telegram_alert('hello'))
            self.assertEqual(post.call_args.kwargs['json'], {'chat_id': '123', 'text': 'hello'})
            self.assertEqual(post.call_args.kwargs['timeout'], 15)

    def test_failure_is_nonfatal_and_does_not_log_token(self):
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'private-token', 'TELEGRAM_CHAT_ID': '123'}), patch('batch_enrich_cron.requests.post', side_effect=RuntimeError('private-token')), self.assertLogs('batch_enrich_cron') as logs:
            self.assertFalse(send_telegram_alert('hello'))
            self.assertNotIn('private-token', '\n'.join(logs.output))

    def test_api_rejection_is_nonfatal(self):
        with patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'test-token', 'TELEGRAM_CHAT_ID': '123'}), patch('batch_enrich_cron.requests.post') as post:
            post.return_value.json.return_value = {'ok': False}
            self.assertFalse(send_telegram_alert('hello'))

    def run_main(self, result=0, error=None, quota=False, count_error=None):
        env = {'SUPABASE_URL': 'https://example.supabase.co', 'SUPABASE_KEY': 'sb_secret_test',
               'TOUR_API_DECODING_KEY': 'tour-key'}
        with patch.dict('os.environ', env, clear=True), patch('sys.argv', ['batch_enrich_cron.py']), patch('batch_enrich_cron.load_dotenv'), patch('batch_enrich_cron.create_client') as client, patch('batch_enrich_cron.Batch') as batch_class, patch('batch_enrich_cron.send_telegram_alert') as alert:
            batch = batch_class.return_value
            batch.run.return_value = result
            batch.run.side_effect = error
            batch.processed = 7
            batch.errors = ['content_id=123 (Timeout)']
            batch.quota_stopped = quota
            batch.completion_counts.return_value = (37, 1773)
            batch.completion_counts.side_effect = count_error
            code = main()
            client.assert_called_once_with(env['SUPABASE_URL'], env['SUPABASE_KEY'])
            return code, [call.args[0] for call in alert.call_args_list]

    def test_main_success_actual_counts_and_kst(self):
        code, messages = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn('수집 시작', messages[0])
        self.assertIn('당일 처리: 7건', messages[1])
        self.assertIn('누적 완료: 37건', messages[1])
        self.assertIn('잔여 대상: 1773건', messages[1])
        self.assertIn('(KST)', messages[1])

    def test_partial_failure_reports_error(self):
        code, messages = self.run_main(result=1)
        self.assertEqual(code, 0)
        self.assertEqual(len(messages), 3)
        self.assertIn('부분 완료 (성공: 7건 / 실패: 1건)', messages[1])
        self.assertIn('37건', messages[1])
        self.assertIn('1773건', messages[1])
        self.assertIn('에러 발생', messages[-1])
        self.assertIn('content_id=123', messages[-1])

    def test_exception_reports_error_without_raw_secret(self):
        code, messages = self.run_main(error=RuntimeError('private-key'))
        self.assertEqual(code, 1)
        self.assertIn('RuntimeError', messages[-1])
        self.assertNotIn('private-key', messages[-1])

    def test_safe_quota_stop_does_not_claim_full_success(self):
        code, messages = self.run_main(result=1, quota=True)
        self.assertEqual(code, 0)
        self.assertIn('일일 안전 한도 도달', messages[1])

    def test_safe_stop_with_failed_counts_still_notifies_and_succeeds(self):
        code, messages = self.run_main(quota=True, count_error=RuntimeError('count unavailable'))
        self.assertEqual(code, 0)
        self.assertIn('집계 불가', messages[1])
        self.assertIn('일일 안전 한도 도달', messages[1])


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.batch = Batch(self.db, 'tour-key', 'https://example.supabase.co', Mock())
        self.db.rpc.return_value.execute.return_value.data = True
        self.db.storage.get_bucket.return_value = {'public': True}

    def test_missing_environment_identifies_secret(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'SUPABASE_URL'):
                load_batch_config()

    def test_url_used_as_key_is_rejected(self):
        with patch.dict('os.environ', {'SUPABASE_URL': 'https://example.supabase.co',
             'SUPABASE_SERVICE_ROLE_KEY': 'https://example.supabase.co',
             'TOUR_API_DECODING_KEY': 'tour-key'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'Invalid Supabase key format'):
                load_batch_config()

    def test_success_does_not_reserve_quota_or_call_tourapi(self):
        self.batch.preflight()
        self.db.rpc.assert_called_once_with('preflight_enrich_batch', {})
        self.batch.http.get.assert_not_called()

    def test_missing_columns_preserves_original_error(self):
        self.db.table.return_value.select.return_value.limit.return_value.execute.side_effect = RuntimeError('column enrichment_completed does not exist')
        with self.assertRaisesRegex(RuntimeError, 'public.places.*column enrichment_completed') as raised:
            self.batch.preflight()
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        self.db.rpc.assert_not_called()

    def test_missing_rpc_identifies_setup_sql(self):
        self.db.rpc.return_value.execute.side_effect = RuntimeError('function not found')
        with self.assertRaisesRegex(RuntimeError, 'sql/enrich_batch_setup.sql'):
            self.batch.preflight()
        self.db.storage.get_bucket.assert_not_called()

    def test_private_bucket_stops_before_tourapi(self):
        self.db.storage.get_bucket.return_value = {'public': False}
        with self.assertRaisesRegex(RuntimeError, 'not public'):
            self.batch.preflight()
        self.batch.http.get.assert_not_called()

    def test_main_prints_original_error_and_traceback(self):
        env = {'SUPABASE_URL': 'https://example.supabase.co', 'SUPABASE_KEY': 'sb_secret_test',
               'TOUR_API_DECODING_KEY': 'tour-key'}
        with patch.dict('os.environ', env, clear=True), patch('sys.argv', ['batch_enrich_cron.py']), patch('batch_enrich_cron.load_dotenv'), patch('batch_enrich_cron.send_telegram_alert'), patch('batch_enrich_cron.create_client', side_effect=RuntimeError('Invalid API key')), patch('batch_enrich_cron.traceback.print_exc') as trace, self.assertLogs('batch_enrich_cron', level='ERROR') as logs:
            self.assertEqual(main(), 1)
            trace.assert_called_once_with()
            self.assertIn('Invalid API key', '\n'.join(logs.output))


class EndpointQuotaTests(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.batch = Batch(self.db, 'tour-key', 'https://example.supabase.co', Mock())
        self.counts = dict.fromkeys(('detailCommon2', 'detailIntro2', 'detailInfo2'), 0)

        def rpc(name, params):
            self.assertEqual(name, 'reserve_enrich_endpoint_budget')
            endpoint = params['p_endpoint']
            allowed = self.counts[endpoint] < 950
            if allowed:
                self.counts[endpoint] += 1
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=allowed))
        self.db.rpc.side_effect = rpc

    def test_800_places_use_independent_counters_and_complete_loop(self):
        self.batch.preflight = Mock()
        query = self.db.table.return_value.select.return_value.eq.return_value.eq.return_value.or_.return_value.order.return_value.order.return_value.limit.return_value
        query.execute.return_value.data = [{'content_id': str(i)} for i in range(800)]
        def enrich(place):
            self.batch.reserve('detailCommon2')
            self.batch.reserve('detailIntro2')
        self.batch.enrich = Mock(side_effect=enrich)
        with patch('batch_enrich_cron.LOG'):
            self.assertEqual(self.batch.run(800), 0)
        self.assertEqual(self.batch.processed, 800)
        self.assertEqual(self.counts, {'detailCommon2': 800, 'detailIntro2': 800, 'detailInfo2': 0})
        self.assertFalse(self.batch.quota_stopped)

    def test_950_allowed_951_blocked_and_other_endpoint_independent(self):
        for _ in range(950):
            self.batch.reserve('detailCommon2')
        with self.assertRaises(BudgetExhausted):
            self.batch.reserve('detailCommon2')
        self.batch.reserve('detailIntro2')
        self.assertEqual(self.counts['detailCommon2'], 950)
        self.assertEqual(self.counts['detailIntro2'], 1)

    def test_new_process_uses_persisted_endpoint_budget(self):
        self.counts['detailIntro2'] = 950
        resumed = Batch(self.db, 'tour-key', 'https://example.supabase.co')
        with self.assertRaises(BudgetExhausted):
            resumed.reserve('detailIntro2')

    def test_tourapi_timeout_is_five_seconds(self):
        self.batch.http.get.return_value.status_code = 429
        with self.assertRaises(RuntimeError):
            self.batch.detail('detailCommon2', {'content_id': '123'})
        self.assertEqual(self.batch.http.get.call_args.kwargs['timeout'], 5)


if __name__ == '__main__':
    unittest.main()
