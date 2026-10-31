import json
import unittest
from unittest.mock import Mock, patch
from pipeline_task2 import Gemini, validate_summary
from scripts.review_evidence import evidence_catalog, basic_summary, quote_diagnostics
from batch_ai_summary_cron import run


class EvidenceRefreshTests(unittest.TestCase):
    def reviews(self):
        return [{'source_id': 1, 'text': '주차장과 산책로가 마련되어 있는 공원입니다.'}]

    def data(self, points=None):
        return {'quality_status': 'ready', 'summary_lines': ['공원 안내', '산책로 안내', '위치 안내'],
                'pros': points or [], 'cons': [], 'visit_tips': '방문 전 운영시간을 확인하세요.'}

    def model(self, bodies):
        session = Mock()
        session.post.side_effect = [Mock(status_code=200, json=lambda data=data: {
            'candidates': [{'content': {'parts': [{'text': json.dumps(data)}]}}]}) for data in bodies]
        return Gemini(session, 'key', min_interval=0, max_rpm=100), session

    def test_empty_pros_and_cons_do_not_exclude_summary(self):
        model, session = self.model([self.data()])
        summary, _ = model.summarize({}, self.reviews())
        self.assertEqual(summary['pros'], [])
        self.assertEqual(summary['cons'], [])
        self.assertEqual(session.post.call_count, 1)

    def test_evidence_id_resolves_to_exact_source_quote(self):
        catalog = evidence_catalog(self.reviews())
        evidence_id = next(iter(catalog))
        model, _ = self.model([self.data([{'text': '산책로가 있습니다.', 'evidence_id': evidence_id}])])
        summary, _ = model.summarize({}, self.reviews())
        self.assertEqual(summary['pros'][0]['evidence'], catalog[evidence_id]['evidence'])
        self.assertEqual(summary['pros'][0]['source_id'], 1)

    def test_invalid_id_gets_one_correction_using_same_material(self):
        bad = self.data([{'text': '없는 내용', 'evidence_id': 'invented-id'}])
        evidence_id = next(iter(evidence_catalog(self.reviews())))
        good = self.data([{'text': '산책로', 'evidence_id': evidence_id}])
        model, session = self.model([bad, good])
        summary, _ = model.summarize({}, self.reviews())
        self.assertEqual(len(summary['pros']), 1)
        self.assertTrue(model.repair_attempted)
        self.assertEqual(session.post.call_count, 2)
        prompts = [call.kwargs['json']['contents'][0] for call in session.post.call_args_list]
        self.assertEqual(prompts[0], prompts[1])
        self.assertEqual(model.validation_events[0]['code'], 'unknown_evidence_id')

    def test_second_invalid_point_does_not_discard_valid_summary(self):
        bad = self.data([{'text': 'unsupported', 'evidence_id': 'missing'}])
        model, session = self.model([bad, bad])
        summary, _ = model.summarize({}, self.reviews())
        self.assertEqual(summary['pros'], [])
        self.assertEqual(session.post.call_count, 2)
        self.assertGreaterEqual(len(model.validation_events), 2)

    def test_broken_correction_preserves_initial_usable_summary(self):
        bad = self.data([{'text': 'unsupported', 'evidence_id': 'missing'}])
        correction = {**self.data(), 'summary_lines': []}
        model, session = self.model([bad, correction])
        summary, _ = model.summarize({}, self.reviews())
        self.assertEqual(len(summary['summary_lines']), 3)
        self.assertEqual(summary['pros'], [])
        self.assertEqual(session.post.call_count, 2)

    def test_diagnostics_distinguish_short_missing_and_unmatched_quotes(self):
        data = self.data([{'text':'a','source_id':1,'evidence':'짧음'},
                          {'text':'b','source_id':1},
                          {'text':'c','source_id':1,'evidence':'원문에 존재하지 않는 새로운 문장입니다.'}])
        codes=[item['code'] for item in quote_diagnostics(data,self.reviews())]
        self.assertEqual(codes, ['short_quote','missing_quote','quote_not_found'])

    def test_no_review_evidence_is_labeled_basic_info_not_fake_review(self):
        db, generator, session = Mock(), Mock(), Mock()
        generator.summarize.side_effect = ValueError('No unsponsored readable reviews')
        place = {'content_id': '1', 'title': '공원', 'address': '서울 중구'}
        with patch('batch_ai_summary_cron.pending_places', return_value=[place]), \
             patch('batch_ai_summary_cron.resume_saved', return_value=None), \
             patch('batch_ai_summary_cron.search_reviews', return_value=[]), \
             patch('batch_ai_summary_cron.persist', return_value=True) as save:
            counts = run(db, session, generator, 'key', 1, sleep=Mock())
        self.assertEqual(counts['basic_info'], 1)
        self.assertEqual(counts['excluded'], 0)
        summary = save.call_args.args[2]
        self.assertEqual(summary['analysis_basis'], 'basic_info')
        self.assertEqual(summary['pros'], [])
        self.assertIsNone(save.call_args.args[5])

    def test_basic_info_includes_analysis_time_and_source_limitation(self):
        data = basic_summary({'title': '공원'}, 'no_reviews')
        self.assertIn('analyzed_at', data)
        self.assertIn('기본 장소 정보', data['summary_lines'][2])
        validate_summary(data, [])
