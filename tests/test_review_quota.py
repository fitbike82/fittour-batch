import contextlib,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
import requests
from pipeline_task2 import Gemini,MODEL_CHAIN,GeminiQuotaDeferred,GeminiCircuitOpen,quota_details
from batch_ai_summary_cron import run,TimeBudgetExpired


def ready():
    quote='Parking is narrow and waiting takes a long time.'
    point={'text':'Allow time for parking','source_id':1,'evidence':quote}
    data={'quality_status':'ready','summary_lines':['a','b','c'],'pros':[point.copy(),point.copy()],
          'cons':[point.copy()],'visit_tips':'Visit early.'}
    return Mock(status_code=200,json=lambda:{'candidates':[{'content':{'parts':[{'text':json.dumps(data)}]}}]}),[{'source_id':1,'text':quote}]


class ReviewQuotaTests(unittest.TestCase):
    def generator(self,responses):
        session=Mock();session.post.side_effect=responses
        return Gemini(session,'secret',sleep=Mock(),clock=lambda:0,max_rpm=1000)

    def test_four_distinct_models_and_exact_2111_request_budget(self):
        response,reviews=ready()
        model=self.generator([Mock(status_code=503)]*4+[response])
        _,actual=model.summarize({},reviews)
        self.assertEqual(actual,MODEL_CHAIN[-1][0])
        self.assertEqual([c.args[0].split('/')[-1].split(':')[0] for c in model.session.post.call_args_list],
                         [MODEL_CHAIN[0][0]]*2+[m for m,_ in MODEL_CHAIN[1:]])
        self.assertEqual(len(set(m for m,_ in MODEL_CHAIN)),4)
        self.assertTrue(all(c.kwargs['timeout']==45 for c in model.session.post.call_args_list))
        for call in model.session.post.call_args_list[-2:]:
            self.assertEqual(call.kwargs['json']['generationConfig']['thinkingConfig'],{'thinkingLevel':'low'})

    def test_all_quota_failures_defer_without_attempt_consumption(self):
        _,reviews=ready();model=self.generator([Mock(status_code=429)]*5)
        with self.assertRaises(GeminiQuotaDeferred):model.summarize({},reviews)
        self.assertEqual(model.quota_429_total,5)
        self.assertEqual(model.session.post.call_count,5)

    def test_mixed_quota_and_server_failures_still_defer(self):
        _,reviews=ready();model=self.generator([Mock(status_code=429)]+[Mock(status_code=503)]*4)
        with self.assertRaises(GeminiQuotaDeferred):model.summarize({},reviews)

    def test_twentieth_429_stops_batch_immediately_and_releases_all_work(self):
        _,reviews=ready();model=self.generator([Mock(status_code=429)]*25)
        places=[{'content_id':str(i),'title':'park','claim_token':'token','attempt_number':2} for i in range(6)]
        db=Mock()
        with patch('batch_ai_summary_cron.pending_places',return_value=places), \
             patch('batch_ai_summary_cron.search_reviews',return_value=reviews), \
             patch('batch_ai_summary_cron.persist') as persist,contextlib.redirect_stdout(io.StringIO()):
            counts=run(db,Mock(),model,'key',120,sleep=Mock())
        self.assertEqual(model.session.post.call_count,20)
        self.assertEqual((counts['errors'],counts['processed'],counts['quota_deferred'],counts['deferred']),(0,0,4,6))
        self.assertTrue(counts['quota_stopped'])
        self.assertEqual(counts['quota_429_total'],20)
        self.assertEqual(sum(len(e['gemini_requests']) for e in counts['events']),20)
        persist.assert_not_called()
        db.rpc.assert_called_once_with('release_ai_review_work',{'claim_token':'token'})
        self.assertFalse(any(e['committed'] for e in counts['events']))
        with self.assertRaises(GeminiCircuitOpen):model.request(MODEL_CHAIN[0][0],{})
        self.assertEqual(model.session.post.call_count,20)

    def test_success_resets_streak_but_retains_total_and_works_on_second_model(self):
        response,reviews=ready();model=self.generator([Mock(status_code=429)]*2+[response])
        model.consecutive_429=17
        _,actual=model.summarize({},reviews)
        self.assertEqual(actual,MODEL_CHAIN[1][0])
        self.assertEqual(model.consecutive_429,0)
        self.assertEqual(model.quota_429_total,2)

    def test_twentieth_response_in_middle_of_chain_does_not_call_later_models(self):
        _,reviews=ready();model=self.generator([Mock(status_code=429)]*5)
        model.consecutive_429=18
        with self.assertRaises(GeminiCircuitOpen):model.summarize({},reviews)
        self.assertEqual(model.session.post.call_count,2)

    def test_timeout_budget_is_not_swallowed_by_model_fallback(self):
        _,reviews=ready();model=self.generator([TimeBudgetExpired()])
        with self.assertRaises(TimeBudgetExpired):model.summarize({},reviews)
        self.assertEqual(model.session.post.call_count,1)

    def test_quota_then_time_budget_does_not_double_count_deferred_places(self):
        _,reviews=ready();model=self.generator([Mock(status_code=429)]*5+[TimeBudgetExpired()])
        places=[{'content_id':str(i),'title':'park','claim_token':'token'} for i in range(3)]
        with patch('batch_ai_summary_cron.pending_places',return_value=places), \
             patch('batch_ai_summary_cron.search_reviews',return_value=reviews), \
             patch('batch_ai_summary_cron.persist') as persist,contextlib.redirect_stdout(io.StringIO()):
            counts=run(Mock(),Mock(),model,'key',120,sleep=Mock())
        self.assertEqual(counts['deferred'],3)
        self.assertEqual(counts['quota_deferred'],1)
        persist.assert_not_called()

    def test_kakao_429_is_not_saved_as_place_error(self):
        response=requests.Response();response.status_code=429
        places=[{'content_id':'1','title':'park','claim_token':'token'}]
        with patch('batch_ai_summary_cron.pending_places',return_value=places), \
             patch('batch_ai_summary_cron.search_reviews',side_effect=requests.HTTPError(response=response)), \
             patch('batch_ai_summary_cron.persist') as persist:
            counts=run(Mock(),Mock(),Mock(),'key',120,sleep=Mock())
        self.assertEqual((counts['quota_deferred'],counts['errors'],counts['deferred']),(1,0,1))
        persist.assert_not_called()

    def test_quota_details_do_not_include_project_ids_raw_messages_or_keys(self):
        response=Mock(json=lambda:{'error':{'message':'secret URL', 'details':[
            {'@type':'type.googleapis.com/google.rpc.QuotaFailure','violations':[
                {'quotaMetric':'requests','quotaId':'per_day','quotaValue':'10','quotaDimensions':{'project':'secret'}}]},
            {'@type':'type.googleapis.com/google.rpc.RetryInfo','retryDelay':'30s'}]}})
        info=quota_details(response)
        self.assertEqual(info['retry_delay'],'30s')
        self.assertNotIn('secret',json.dumps(info))

    def test_twenty_kakao_quota_failures_stop_without_consuming_any_attempt(self):
        response=requests.Response();response.status_code=429
        places=[{'content_id':str(i),'title':'park','claim_token':'token'} for i in range(25)]
        db=Mock()
        with patch('batch_ai_summary_cron.pending_places',return_value=places), \
             patch('batch_ai_summary_cron.search_reviews',side_effect=requests.HTTPError(response=response)) as search, \
             patch('batch_ai_summary_cron.persist') as persist:
            counts=run(db,Mock(),Mock(),'key',120,sleep=Mock())
        self.assertEqual(search.call_count,20)
        self.assertEqual((counts['quota_deferred'],counts['errors'],counts['deferred']),(20,0,25))
        self.assertTrue(counts['quota_stopped'])
        persist.assert_not_called()
        db.rpc.assert_called_once_with('release_ai_review_work',{'claim_token':'token'})

    def test_gemini_daily_quota_exhausted_stops_immediately_and_preserves_queue(self):
        daily_err = {
            'error': {
                'code': 429,
                'status': 'RESOURCE_EXHAUSTED',
                'message': 'Quota exceeded for quota metric GenerateContent requests per day and limit PerDayPerProject',
                'details': [
                    {'@type': 'type.googleapis.com/google.rpc.QuotaFailure',
                     'violations': [{'quotaMetric': 'GenerateContent requests per day', 'quotaId': 'per_day'}]}
                ]
            }
        }
        mock_response = Mock(status_code=429)
        mock_response.json.return_value = daily_err
        model = self.generator([mock_response])
        places = [{'content_id': str(i), 'title': f'place_{i}', 'claim_token': f'token_{i}', 'attempt_number': 1} for i in range(10)]
        db = Mock()
        with patch('batch_ai_summary_cron.pending_places', return_value=places), \
             patch('batch_ai_summary_cron.search_reviews', return_value=[{'source_id': 1, 'text': 'review text'}]), \
             patch('batch_ai_summary_cron.persist') as persist, \
             contextlib.redirect_stdout(io.StringIO()):
            counts = run(db, Mock(), model, 'key', 50, sleep=Mock())
        self.assertEqual(model.session.post.call_count, 1)
        self.assertTrue(counts['quota_stopped'])
        self.assertEqual(counts['stop_reason'], 'daily_quota_exceeded')
        self.assertEqual(counts['quota_deferred'], 1)
        self.assertEqual((counts['errors'], counts['processed'], counts['deferred']), (0, 0, 10))
        persist.assert_not_called()
        self.assertEqual(db.rpc.call_count, 10)
        self.assertTrue(all(c.args[0] == 'release_ai_review_work' for c in db.rpc.call_args_list))

    def test_mask_sensitive_credentials_in_urls_headers_and_logs(self):
        from pipeline_task2 import mask_sensitive
        url1 = "https://apis.data.go.kr/1261000/service?serviceKey=SECRET_SERVICE_KEY%3D%3D&pageNo=1"
        self.assertEqual(mask_sensitive(url1), "https://apis.data.go.kr/1261000/service?serviceKey=***&pageNo=1")
        url2 = "https://generativelanguage.googleapis.com/v1beta/models/gemini:generateContent?key=AIzaSySecretKey"
        self.assertEqual(mask_sensitive(url2), "https://generativelanguage.googleapis.com/v1beta/models/gemini:generateContent?key=***")
        url3 = "https://example.com/api?apikey=SecretApiKey123&token=SecretToken456"
        self.assertEqual(mask_sensitive(url3), "https://example.com/api?apikey=***&token=***")
        header1 = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret"
        self.assertEqual(mask_sensitive(header1), "Authorization: Bearer ***")
        header2 = "Authorization: KakaoAK kakao_secret_12345"
        self.assertEqual(mask_sensitive(header2), "Authorization: KakaoAK ***")
