import json
from pathlib import Path
import re
import unittest
from unittest.mock import Mock, patch
import requests
from batch_ai_summary_cron import (search_reviews, ReviewCollectionUnavailable, run, persist,
                                    resume_saved, TimeBudgetExpired, safe_error_code)
from pipeline_task2 import review_excerpt, validate_summary


class ReviewRecoveryTests(unittest.TestCase):
    def test_deleted_article_does_not_discard_other_sources(self):
        session=Mock()
        session.get.return_value.json.return_value={'documents':[{'url':u} for u in ['a','bad','c','d']]}
        response=requests.Response();response.status_code=404
        error=requests.HTTPError(response=response)
        with patch('batch_ai_summary_cron.read_article',side_effect=['source a',error,'source c','source d']):
            rows=search_reviews(session,'key','place')
        self.assertEqual([r['url'] for r in rows],['a','c','d'])
        self.assertEqual(session.review_diagnostics['article_errors'],{'404':1})
        self.assertEqual(session.get.call_count,1)

    def test_temporary_source_failure_is_retryable_not_permanent_exclusion(self):
        session=Mock()
        session.get.return_value.json.side_effect=[{'documents':[{'url':u}]} for u in ['a','b','c']]
        with patch('batch_ai_summary_cron.read_article',side_effect=requests.Timeout):
            with self.assertRaises(ReviewCollectionUnavailable):
                search_reviews(session,'key','place')
        self.assertEqual(session.review_diagnostics['readable'],0)

    def test_valid_alternatives_survive_temporary_errors(self):
        session=Mock()
        session.get.return_value.json.return_value={'documents':[{'url':u} for u in ['a','b','c']]}
        with patch('batch_ai_summary_cron.read_article',side_effect=[requests.Timeout(),'body one','body two']):
            self.assertEqual(len(search_reviews(session,'key','place')),2)

    def test_excerpt_preserves_late_visit_constraint_inside_budget(self):
        quote='주차장이 협소해서 대기 시간이 오래 걸렸어요.'
        text='앞부분 일반 소개입니다. '*100+quote+' 주변 안내입니다.'*40
        excerpt=review_excerpt(text)
        self.assertEqual(len(excerpt),500)
        self.assertIn(quote,excerpt)
        self.assertIn(excerpt,text)

    def summary(self):
        quote='주차 공간이 좁아서 기다렸어요'
        p={'text':'주차가 불편해요','source_id':1,'evidence':quote}
        return {'quality_status':'ready','summary_lines':['one','two','three'],
                'pros':[p.copy(),p.copy()],'cons':[p.copy()],'visit_tips':'tip'},quote

    def test_whitespace_and_unique_source_id_can_be_repaired_without_invention(self):
        data,quote=self.summary()
        data['cons'][0]['source_id']=99
        rows=[{'source_id':1,'text':quote.replace(' ','\n')}]
        result=validate_summary(data,rows)
        self.assertEqual(result['cons'][0]['source_id'],1)
        self.assertEqual(result['cons'][0]['evidence'],rows[0]['text'])

    def test_ambiguous_source_id_and_invented_evidence_stay_excluded(self):
        data,quote=self.summary();data['cons'][0]['source_id']=99
        with self.assertRaisesRegex(ValueError,'Ungrounded'):
            validate_summary(data,[{'source_id':i,'text':quote} for i in [1,2]])
        data,quote=self.summary();data['cons'][0]['evidence']='원문에 존재하지 않는 실제로 지어낸 문장이에요'
        with self.assertRaisesRegex(ValueError,'Ungrounded'):
            validate_summary(data,[{'source_id':1,'text':quote}])

    def test_retry_does_not_resume_last_pass_rejection(self):
        db=Mock()
        place={'content_id':'1','claim_token':'lease','attempt_number':2}
        self.assertIsNone(resume_saved(db,place));db.table.assert_not_called()

    def test_result_and_attempt_are_written_by_one_atomic_rpc(self):
        db=Mock();db.rpc.return_value.execute.return_value.data=True
        place={'content_id':'1','claim_token':'lease','modified_time':'v1'}
        self.assertTrue(persist(db,place,None,[],'insufficient','model','insufficient_evidence'))
        self.assertEqual(db.rpc.call_args.args[0],'complete_ai_review_work')
        db.table.assert_not_called()

    def test_time_budget_releases_unstarted_jobs_without_completing_attempt(self):
        db=Mock();session=Mock();generator=Mock()
        places=[{'content_id':str(i),'title':'place','claim_token':'lease','attempt_number':1} for i in range(2)]
        with patch('batch_ai_summary_cron.pending_places',return_value=places), \
             patch('batch_ai_summary_cron.search_reviews',side_effect=TimeBudgetExpired):
            counts=run(db,session,generator,'key',120,sleep=Mock())
        self.assertEqual(counts['deferred'],2)
        db.rpc.assert_called_once_with('release_ai_review_work',{'claim_token':'lease'})
        generator.summarize.assert_not_called()

    def test_error_attempt_is_recorded_then_later_places_continue(self):
        db=Mock();db.rpc.return_value.execute.return_value.data=True
        generator=Mock();generator.summarize.side_effect=[RuntimeError('sensitive raw URL'),(self.summary()[0],'model')]
        places=[{'content_id':str(i),'title':'place','claim_token':'lease','attempt_number':1} for i in range(2)]
        with patch('batch_ai_summary_cron.pending_places',return_value=places), \
             patch('batch_ai_summary_cron.search_reviews',return_value=[]):
            counts=run(db,Mock(),generator,'key',120,sleep=Mock())
        self.assertEqual((counts['errors'],counts['success']),(1,1))
        self.assertEqual([c.args[1]['result_status'] for c in db.rpc.call_args_list if c.args[0]=='complete_ai_review_work'],['error','ready'])
        self.assertNotIn('sensitive',json.dumps(counts))

    def test_kst_schedule_runs_at_every_hour_once(self):
        text = Path('.github/workflows/daily_ai_summary_batch.yml').read_text(encoding='utf-8')
        cron_expr = re.search(r"cron: '([^']+)'", text).group(1)
        cron_parts = cron_expr.split()
        self.assertEqual(cron_parts[0], '0')
        hour_field = cron_parts[1]
        utc_hours = []
        for part in hour_field.split(','):
            if '-' in part:
                start, end = map(int, part.split('-'))
                utc_hours.extend(range(start, end + 1))
            else:
                utc_hours.append(int(part))
        self.assertEqual(len(utc_hours), 20)
        kst_hours = [(hour + 9) % 24 for hour in utc_hours]
        self.assertEqual(len(kst_hours), 20)
        for maintenance_hour in (2, 3, 4, 5):
            self.assertNotIn(maintenance_hour, kst_hours)
        self.assertIn('--limit 50', text)
        self.assertIn('timeout-minutes: 35', text)

    def test_error_messages_never_include_raw_exception_secrets(self):
        self.assertEqual(safe_error_code(RuntimeError('https://secret.example?key=SECRET')),'RuntimeError')
