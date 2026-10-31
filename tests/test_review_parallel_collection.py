import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from scripts.smart_search import parallel_review_pages
from batch_ai_summary_cron import search_reviews, read_article, review_region, write_report
from pipeline_task2 import Gemini
from tests.test_review_quota import ready


class ParallelCollectionTests(unittest.TestCase):
    @patch.dict(os.environ, {'NAVER_CLIENT_ID': 'id', 'NAVER_CLIENT_SECRET': 'secret'}, clear=True)
    def test_three_search_endpoints_start_concurrently_and_use_region(self):
        barrier = threading.Barrier(3)
        session = Mock()
        def get(url, **kwargs):
            barrier.wait(timeout=5)
            self.assertEqual(kwargs['params']['query'], '서울특별시 중구 공원 후기')
            self.assertEqual(kwargs['params'].get('display', kwargs['params'].get('size')), 5)
            return Mock(json=lambda: {'documents': []} if 'kakao' in url else {'items': []})
        session.get.side_effect = get
        pages = list(parallel_review_pages(session, 'key', '공원', ['후기'], sido='서울특별시', sigungu='중구'))
        self.assertEqual(len(pages[0]), 3)
        self.assertEqual({p['kind'] for p in pages[0]}, {'blog', 'cafearticle'})

    def test_bad_articles_are_replaced_and_both_provider_quotas_reach_five(self):
        def page(provider, kind, urls):
            return {'provider': provider, 'kind': kind, 'query': '서울 공원 후기',
                    'documents': [{'url': url, 'title': '공원'} for url in urls]}
        rounds = [[page('naver', 'blog', ['bad', 'n1', 'n2', 'n3', 'n4']),
                   page('naver', 'cafearticle', ['c1', 'c2', 'c3', 'c4', 'c5']),
                   page('kakao', 'blog', ['k1', 'k2', 'k3', 'k4', 'k5'])]]
        def article(session, url):
            if url == 'bad':
                raise ValueError('Sponsored article')
            return '본문 ' * 300
        session = Mock()
        with patch('batch_ai_summary_cron.parallel_review_pages', return_value=iter(rounds)), \
             patch('batch_ai_summary_cron.read_article', side_effect=article):
            reviews = search_reviews(session, 'key', '공원')
        self.assertEqual(len(reviews), 10)
        self.assertEqual(session.review_diagnostics['sources'], {'naver': 5, 'kakao': 5})
        self.assertEqual(session.review_diagnostics['filtered'], {'Sponsored article': 1})
        self.assertTrue(any(r['kind'] == 'cafearticle' for r in reviews))
        self.assertEqual([r['source_id'] for r in reviews], list(range(1, 11)))

    def test_same_url_from_both_providers_is_not_counted_twice(self):
        pages = [[{'provider': p, 'kind': 'blog', 'query': 'q', 'documents': [{'url': 'same'}]}
                  for p in ('naver', 'kakao')]]
        with patch('batch_ai_summary_cron.parallel_review_pages', return_value=iter(pages)), \
             patch('batch_ai_summary_cron.read_article', return_value='본문'):
            self.assertEqual(len(search_reviews(Mock(), 'key', 'place')), 1)

    def test_public_cafe_body_and_unavailable_cafe_are_distinct(self):
        session = Mock()
        body = '실제 방문 후기와 주차장에 대한 자세한 설명입니다. ' * 40
        session.get.return_value.content = f'<div class="ContentRenderer">{body}</div>'.encode()
        self.assertGreater(len(read_article(session, 'https://cafe.naver.com/test/1')), 500)
        session.get.return_value.content = b'<html>Login required</html>'
        with self.assertRaisesRegex(ValueError, 'Cafe public body unavailable'):
            read_article(session, 'https://cafe.naver.com/test/2')

    def test_address_is_loaded_when_queue_does_not_include_region(self):
        db = Mock()
        db.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = [
            {'addr1': '부산광역시 해운대구 해운대로 1', 'address': None}]
        self.assertEqual(review_region(db, {'content_id': '1', 'claim_token': 'token'}), ('부산광역시', '해운대구'))

    def test_gemini_receives_all_ten_long_bodies(self):
        response, _ = ready()
        session = Mock()
        session.post.return_value = response
        generator = Gemini(session, 'key')
        quote = 'Parking is narrow and waiting takes a long time.'
        reviews = [{'source_id': i, 'title': str(i), 'text': quote + ' description' * 200} for i in range(1, 13)]
        generator.summarize({}, reviews)
        payload = session.post.call_args.kwargs['json']
        supplied = json.loads(payload['contents'][0]['parts'][0]['text'])['reviews']
        self.assertEqual(len(supplied), 10)
        self.assertTrue(all(500 < len(r['text']) <= 3000 for r in supplied))

    def test_report_separates_quality_collection_and_provider_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            write_report(path, {'events': [
                {'status': 'insufficient', 'reason': 'Ungrounded cons',
                 'diagnostics': {'filtered': {'Cafe public body unavailable': 2}}},
                {'status': 'error', 'reason': 'review_collection_unavailable',
                 'diagnostics': {'search_errors': {'naver_cafearticle_403': 1}}}]})
            report = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(report['exclusion_reasons'], {'quote_validation': 1})
            self.assertEqual(report['body_filter_reasons'], {'Cafe public body unavailable': 2})
            self.assertEqual(report['failure_reasons'], {'review_collection_unavailable': 1})


if __name__ == '__main__':
    unittest.main()
