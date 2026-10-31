import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pipeline_task2 import sponsored, validate_summary, search_kakao_blogs, Gemini, sample_places


@patch.dict(os.environ, {}, clear=True)
class QualityTests(unittest.TestCase):
    def test_sponsorship_spacing_and_html_cleaning(self):
        self.assertTrue(sponsored('업체에서 제 공 받 아 작성'))
        self.assertTrue(sponsored('소정의 비용'))
        self.assertFalse(sponsored('직접 결제하고 방문했어요'))

    def test_fallback_after_filter_and_deduplicate(self):
        from unittest.mock import patch
        session = Mock()
        response = Mock()
        response.json.side_effect = [
            {'documents': [{'url': 'a', 'title': '협찬'}, {'url': 'b', 'title': '내돈내산'}]},
            {'documents': [{'url': 'b'}, {'url': 'c'}, {'url': 'd'}]}]
        session.get.return_value = response
        with patch('pipeline_task2.read_blog', return_value='원문 리뷰'):
            reviews, dropped = search_kakao_blogs(session, 'key', '장소')
        self.assertEqual(len(reviews), 3)
        self.assertEqual(dropped, 1)
        self.assertTrue(all(call.args[0] == 'https://dapi.kakao.com/v2/search/blog'
                            for call in session.get.call_args_list))
        self.assertEqual([c.kwargs['params']['query'] for c in session.get.call_args_list],
                         ['장소 내돈내산', '장소 솔직 후기'])

    def valid(self):
        quote = '주차 공간이 좁아서 기다렸어요'
        point = {'text': '주차 공간이 좁아요', 'source_id': 1, 'evidence': quote}
        return {'quality_status': 'ready', 'summary_lines': ['첫째', '둘째', '셋째'],
                'pros': [point.copy(), point.copy()], 'cons': [point.copy()], 'visit_tips': '대중교통을 이용하세요.'}, [{'source_id': 1, 'text': quote}]

    def test_two_valid_reviews_do_not_trigger_second_search(self):
        from unittest.mock import patch
        session=Mock()
        session.get.return_value.json.return_value={'documents':[{'url':'a'},{'url':'b'}]}
        with patch('pipeline_task2.read_blog',return_value='유효 원문'):
            reviews,_=search_kakao_blogs(session,'key','장소')
        self.assertEqual(len(reviews),2)
        self.assertEqual(session.get.call_count,1)

    def test_missing_cons_and_invented_quotes_rejected(self):
        data, reviews = self.valid()
        self.assertEqual(validate_summary(data, reviews), data)
        data['cons'] = []
        with self.assertRaises(ValueError): validate_summary(data, reviews)
        data, reviews = self.valid()
        data['cons'][0]['evidence'] = '원문에 없는 내용으로 만든 단점'
        with self.assertRaises(ValueError): validate_summary(data, reviews)

    def test_insufficient_evidence_rejected(self):
        data, reviews = self.valid()
        data['quality_status'] = 'insufficient_evidence'
        with self.assertRaises(ValueError): validate_summary(data, reviews)

    def test_rate_limit_and_json_mode_no_model_probes(self):
        import json
        data, reviews = self.valid()
        response = Mock(status_code=200)
        response.json.return_value = {'candidates': [{'content': {'parts': [{'text': json.dumps(data)}]}}]}
        session, sleep = Mock(), Mock()
        session.post.return_value = response
        model = Gemini(session, 'key', sleep=sleep, clock=lambda: 1)
        model.summarize({}, reviews)
        model.summarize({}, reviews)
        sleep.assert_not_called()
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(session.post.call_args.kwargs['json']['generationConfig']['responseMimeType'], 'application/json')

    def test_503_429_retry_twice_then_continue_caller(self):
        session,sleep=Mock(),Mock()
        session.post.side_effect=[Mock(status_code=503),Mock(status_code=429),
                                  Mock(status_code=503)]
        model=Gemini(session,'key',sleep=sleep,clock=lambda:1)
        with self.assertRaises(RuntimeError): model.request('test-model',{},max_retries=2)
        self.assertEqual(session.post.call_count,3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list],[5,10])

    def test_google_api_core_exceptions_are_retried(self):
        from google.api_core.exceptions import ServiceUnavailable,ResourceExhausted,GoogleAPIError
        session,sleep=Mock(),Mock()
        success=Mock(status_code=200)
        session.post.side_effect=[ServiceUnavailable('503'),ResourceExhausted('429'),success]
        model=Gemini(session,'key',sleep=sleep,clock=lambda:1)
        self.assertIs(model.request('test-model',{},max_retries=2),success)
        self.assertEqual([c.args[0] for c in sleep.call_args_list],[5,10])
        session.post.side_effect=GoogleAPIError('secret must not leak')
        with self.assertRaisesRegex(RuntimeError,'GoogleAPIError'):
            model.request('test-model',{},max_retries=2)

    def test_rolling_quota_includes_retries(self):
        current=[0.0]
        waits=[]
        def sleep(seconds):
            waits.append(seconds)
            current[0]+=seconds
        model=Gemini(Mock(),'key',sleep=sleep,clock=lambda:current[0])
        for _ in range(16): model.wait_for_quota()
        self.assertEqual(len(waits),1)
        self.assertGreaterEqual(waits[0],60)
        self.assertEqual(len(model.calls),1)

    def test_404_is_not_retried_as_transient(self):
        session=Mock()
        session.post.return_value=Mock(status_code=404)
        model=Gemini(session,'key',model='explicit-model',sleep=Mock(),clock=lambda:1)
        from google.api_core.exceptions import NotFound
        with self.assertRaises(NotFound):
            model.summarize({},self.valid()[1])
        self.assertEqual(session.post.call_count,1)
        model.sleep.assert_not_called()

    def test_prefix_budget_and_lite_fallback(self):
        import json
        from pipeline_task2 import MODEL,FALLBACK_MODEL
        data,reviews=self.valid()
        quote=reviews[0]['text']
        reviews=[{'source_id':i+1,'text':quote+'가'*2000} for i in range(8)]
        response=Mock(status_code=200)
        response.json.return_value={'candidates':[{'content':{'parts':[{'text':json.dumps(data)}]}}]}
        session=Mock()
        session.post.side_effect=[Mock(status_code=404),response]
        model=Gemini(session,'key',sleep=Mock(),clock=lambda:1)
        _,actual=model.summarize({},reviews)
        self.assertEqual(actual,FALLBACK_MODEL)
        self.assertIn(MODEL,session.post.call_args_list[0].args[0])
        self.assertIn(FALLBACK_MODEL,session.post.call_args_list[1].args[0])
        prompt=json.loads(session.post.call_args.kwargs['json']['contents'][0]['parts'][0]['text'])
        self.assertEqual(len(prompt['reviews']),8)
        self.assertEqual(sum(len(r['text']) for r in prompt['reviews']),sum(len(r['text']) for r in reviews))
        self.assertTrue(all(500 < len(r['text']) <= 3000 for r in prompt['reviews']))

    def test_three_place_category_mix(self):
        db=Mock()
        db.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.range.return_value.execute.side_effect=[
            SimpleNamespace(data=[{'content_id':str(c),'content_type_id':c}]) for c in (12,39,32)]
        self.assertEqual([p['content_type_id'] for p in sample_places(db,3,seed=9)],[12,39,32])

    def test_batch_continues_after_failure_and_always_sleeps_five(self):
        import contextlib
        import io
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from pipeline_task2 import main
        data,reviews=self.valid()
        places=[{'content_id':str(i),'title':'테스트','content_type_id':12} for i in range(10)]
        generator=Mock()
        generator.summarize.side_effect=[RuntimeError('failed')]+[(data,'test-model')]*9
        config={'SUPABASE_URL':'https://example.supabase.co','SUPABASE_SERVICE_ROLE_KEY':'dummy',
                'KAKAO_REST_API_KEY':'dummy','GEMINI_API_KEY':'dummy'}
        with tempfile.TemporaryDirectory() as folder:
            output=str(Path(folder)/'report.json')
            with patch.dict('os.environ',config), patch('pipeline_task2.load_dotenv'), \
                 patch('sys.argv',['pipeline_task2.py','--sample','10','--report',output]), \
                 patch('pipeline_task2.create_client',return_value=Mock()), \
                 patch('pipeline_task2.sample_places',return_value=places), \
                 patch('pipeline_task2.search_kakao_blogs',return_value=(reviews,0)), \
                 patch('pipeline_task2.Gemini',return_value=generator), \
                 patch('pipeline_task2.print_summary'), \
                 patch('pipeline_task2.save_result') as save, \
                 patch('pipeline_task2.time.sleep') as sleep, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(),1)
            self.assertEqual(generator.summarize.call_count,10)
            self.assertEqual(save.call_count,9)
            self.assertEqual([c.args[0] for c in sleep.call_args_list],[5]*10)
            report=json.loads(Path(output).read_text(encoding='utf-8'))
            self.assertEqual((report['saved'],len(report['failures'])),(9,1))

    def test_sample_uses_exact_category_mix(self):
        db = Mock()
        db.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.range.return_value.execute.side_effect = [
            SimpleNamespace(data=[{'content_id': f'{category}-{i}', 'content_type_id': category} for i in range(8)]) for category in (12, 39, 32)]
        places = sample_places(db, 10, seed=9)
        self.assertEqual([sum(p['content_type_id'] == c for p in places) for c in (12, 39, 32)], [3, 4, 3])
        self.assertEqual(len(set(p['content_id'] for p in places)), 10)


if __name__ == '__main__':
    unittest.main()
