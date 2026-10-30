import os
import unittest
from unittest.mock import Mock, patch

import requests
from scripts.smart_search import build_smart_queries, has_search_credentials, smart_search_pages


class SmartSearchTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_missing_response_and_exhausted_mock_end_cleanly(self):
        for responses in ([None], []):
            with self.subTest(responses=responses):
                session = Mock()
                session.get.side_effect = responses
                self.assertEqual(list(smart_search_pages(session, 'key', '맛집', ['후기'])), [])

    @patch.dict(os.environ, {'NAVER_CLIENT_ID': 'id', 'NAVER_CLIENT_SECRET': 'secret'}, clear=True)
    def test_deduplicated_queries_are_reused_for_each_provider(self):
        session = Mock()
        naver, kakao = Mock(), Mock()
        naver.json.return_value = {'items': []}
        kakao.json.return_value = {'documents': []}
        session.get.side_effect = [naver, kakao]
        pages = list(smart_search_pages(session, 'key', '맛집', ['후기', '후기']))
        self.assertEqual(pages, [('맛집 후기', []), ('맛집 후기', [])])
        self.assertEqual([call.args[0] for call in session.get.call_args_list], [
            'https://naverapihub.apigw.ntruss.com/search/v1/blog',
            'https://dapi.kakao.com/v2/search/blog'])

    def test_query_candidates(self):
        self.assertEqual(build_smart_queries('(주) 맛집 [본점] 2호점', '서울특별시', '강남구'), ['강남 맛집', '맛집'])
        self.assertEqual(build_smart_queries('강남 맛집', sigungu='강남구'), ['강남 맛집'])
        self.assertEqual(build_smart_queries('맛집', sido='제주특별자치도'), ['제주 맛집', '맛집'])
        self.assertEqual(build_smart_queries('(주)', sigungu='강남구'), [])

    @patch.dict(os.environ, {'NAVER_CLIENT_ID': 'id', 'NAVER_CLIENT_SECRET': 'secret'}, clear=True)
    def test_naver_first_and_normalized_documents(self):
        session = Mock()
        session.get.return_value.json.return_value = {'items': [{'title': '<b>title</b>', 'link': 'url', 'description': '<b>body</b>', 'postdate': '20261029'}]}
        pages = smart_search_pages(session, 'key', '맛집', ['후기'])
        query, docs = next(pages)
        self.assertEqual(query, '맛집 후기')
        self.assertEqual(docs[0]['url'], 'url')
        self.assertEqual(docs[0]['contents'], 'body')
        self.assertEqual(docs[0]['title'], 'title')
        self.assertEqual(session.get.call_args.args[0], 'https://naverapihub.apigw.ntruss.com/search/v1/blog')
        self.assertEqual(session.get.call_args.kwargs['headers'], {'X-NCP-APIGW-API-KEY-ID': 'id', 'X-NCP-APIGW-API-KEY': 'secret'})
        self.assertEqual(session.get.call_args.kwargs['params'], {'query': '맛집 후기', 'display': 5, 'sort': 'sim', 'format': 'json'})
        self.assertTrue(has_search_credentials())

    @patch.dict(os.environ, {'NAVER_CLIENT_ID': 'id', 'NAVER_CLIENT_SECRET': 'secret'}, clear=True)
    def test_naver_error_falls_back_and_kakao_quota_error_is_preserved(self):
        session = Mock()
        naver, kakao = Mock(), Mock()
        naver.raise_for_status.side_effect = requests.HTTPError(response=Mock(status_code=401))
        kakao.json.return_value = {'documents': [{'url': 'url'}]}
        session.get.side_effect = [naver, kakao]
        self.assertEqual(list(smart_search_pages(session, 'key', '맛집', ['후기']))[0][1], [{'url': 'url'}])
        self.assertIn('dapi.kakao.com', session.get.call_args.args[0])
        quota = requests.HTTPError(response=Mock(status_code=429))
        kakao.raise_for_status.side_effect = quota
        session.get.side_effect = [naver, kakao]
        with self.assertRaises(requests.HTTPError) as caught:
            list(smart_search_pages(session, 'key', '맛집', ['후기']))
        self.assertIs(caught.exception, quota)

    @patch.dict(os.environ, {'NAVER_CLIENT_ID': 'id'}, clear=True)
    def test_partial_naver_configuration_uses_kakao_and_empty_titles_make_no_calls(self):
        session = Mock()
        session.get.return_value.json.return_value = {'documents': []}
        list(smart_search_pages(session, 'key', '맛집', ['후기'], sigungu='강남구'))
        self.assertEqual([c.kwargs['params']['query'] for c in session.get.call_args_list], ['강남 맛집 후기', '맛집 후기'])
        self.assertTrue(all('dapi.kakao.com' in c.args[0] for c in session.get.call_args_list))
        session.reset_mock()
        self.assertEqual(list(smart_search_pages(session, 'key', '(주)', ['후기'])), [])
        session.get.assert_not_called()

    @patch.dict(os.environ, {'NAVER_CLIENT_ID': 'id', 'NAVER_CLIENT_SECRET': 'secret'}, clear=True)
    def test_empty_naver_result_does_not_hide_terminal_kakao_quota(self):
        session = Mock()
        naver, kakao = Mock(), Mock()
        naver.json.return_value = {'items': []}
        kakao.raise_for_status.side_effect = requests.HTTPError(response=Mock(status_code=429))
        session.get.side_effect = [naver, kakao]
        pages = smart_search_pages(session, 'key', '맛집', ['후기'])
        self.assertEqual(next(pages), ('맛집 후기', []))
        with self.assertRaises(requests.HTTPError):
            next(pages)
