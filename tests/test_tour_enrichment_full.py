import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests
from scripts.sync_tour_enrichment_full import ApiFailure, Journal, QuotaStop, TourClient, image_rows, items_from


def payload(items, total=None, code='0000'):
    return {'response': {'header': {'resultCode': code}, 'body': {'items': {'item': items}, 'totalCount': len(items) if total is None else total}}}


class Response:
    def __init__(self, data, status=200):
        self.content = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError('simulated HTTP error')


class Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs['params']))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'journal.sqlite3')
        self.journal = Journal(self.path, 'test-key')
        self.place = {'content_id': '123', 'content_type_id': '39'}

    def tearDown(self):
        self.journal.db.close()
        self.tmp.cleanup()

    def client(self, responses, retries=3):
        client = TourClient('test-key', self.journal, delay=0, retries=retries)
        client.http = Http(responses)
        return client

    def test_singleton_empty_and_malformed_envelopes(self):
        self.assertEqual(items_from(payload({'contentid': '1'}, 1))[0], [{'contentid': '1'}])
        self.assertEqual(items_from(payload('', 0))[0], [])
        self.assertEqual(items_from({'response': {'header': {'resultCode': '03'}}}), ([], 0))
        with self.assertRaises(ValueError):
            items_from({'response': {'header': {'resultCode': '0000'}}})

    def test_stale_900_calls_and_midnight_rollover(self):
        with patch('scripts.sync_tour_enrichment_full.kst_today', return_value='2026-10-18'):
            self.journal.reserve('KorService2')
            self.journal.db.execute('update calls set count=900 where service=? and day=?', ('KorService2', '2026-10-18'))
            self.journal.db.commit()
        with patch('scripts.sync_tour_enrichment_full.kst_today', return_value='2026-10-19'):
            self.assertEqual(self.journal.day_calls()['KorService2'], 0)
            self.journal.reserve('KorService2')
            self.assertEqual(self.journal.day_calls()['KorService2'], 1)

    def test_force_bypasses_local_and_db_budgets_but_records_calls(self):
        from unittest.mock import Mock
        self.journal.service_limit = 1
        self.journal.reserve('KorService2')
        self.journal.force = True
        db = Mock()
        db.rpc.return_value = True
        client = TourClient('test-key', self.journal, db=db, force=True, delay=0)
        client.http = Http([Response(payload([]))])
        client.collect('KorService2', 'detailInfo2', self.place)
        db.rpc.assert_not_called()
        self.assertEqual(self.journal.day_calls()['KorService2'], 2)

    def test_utf8_all_pages_and_cached_resume(self):
        client = self.client([Response(payload([{'infoname': '첫 정보'}], 2)), Response(payload([{'infoname': '둘째 정보'}], 2))])
        pages, items = client.collect('KorService2', 'detailInfo2', self.place)
        self.assertEqual([row['infoname'] for row in items], ['첫 정보', '둘째 정보'])
        self.assertEqual(len(pages), 2)
        self.assertEqual(client.http.calls[1][1]['pageNo'], 2)
        self.assertEqual(client.http.calls[0][1]['contentTypeId'], '39')
        self.assertEqual(client.collect('KorService2', 'detailInfo2', self.place), (pages, items))
        self.assertEqual(len(client.http.calls), 2)

    def test_incomplete_page_never_marks_a_complete_result(self):
        client = self.client([Response(payload([{'x': 'one'}], 2)), Response(payload([], 2))])
        with self.assertRaisesRegex(RuntimeError, 'advertised totalCount'):
            client.collect('KorService2', 'detailInfo2', self.place)

    def test_counters_never_enforce_limits(self):
        self.journal.global_limit = 1
        self.journal.service_limit = 1
        for _ in range(3):
            self.journal.reserve('KorService2')
        other = Journal(self.path, 'test-key', global_limit=1, service_limit=1)
        try:
            other.reserve('KorService2')
            self.assertEqual(other.day_calls()['KorService2'], 4)
        finally:
            other.db.close()

    @patch('scripts.sync_tour_enrichment_full.time.sleep')
    def test_retries_are_charged_before_every_http_attempt(self, _):
        client = self.client([requests.ConnectionError('network')] * 3)
        with self.assertRaises(RuntimeError):
            client.collect('KorService2', 'detailImage2', self.place)
        self.assertEqual(self.journal.db.execute('select sum(count) from calls').fetchone()[0], 3)
        self.assertEqual(len(client.http.calls), 3)

    def test_permission_parameter_and_quota_errors_are_not_empty_success(self):
        for code in ('20', '10'):
            client = self.client([Response({'resultCode': code}, 400)])
            with self.assertRaises(ApiFailure) as caught:
                client.collect('KorWithService2', 'detailWithTour2', self.place)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(len(client.http.calls), 1)
        client = self.client([Response({}, 429)])
        with self.assertRaises(QuotaStop):
            client.collect('KorService2', 'detailImage2', self.place)

    def test_menu_variant_uses_the_same_api_with_image_n(self):
        client = self.client([Response(payload([]))])
        client.collect('KorService2', 'detailImage2.menu', self.place)
        url, params = client.http.calls[0]
        self.assertTrue(url.endswith('/detailImage2'))
        self.assertEqual(params['imageYN'], 'N')
        self.assertNotIn('subImageYN', params)

    def test_refresh_and_revision_keep_all_quota_reservations(self):
        self.journal.revision('123', 'v1')
        self.journal.reserve('KorService2')
        self.journal.save('123', 'KorService2', 'detailInfo2', 1, payload([]))
        self.journal.mark('123', 'KorService2', 'detailInfo2', 'digest')
        self.journal.revision('123', 'v2')
        self.assertIsNone(self.journal.cached('123', 'KorService2', 'detailInfo2', 1))
        self.assertFalse(self.journal.committed('123', 'KorService2', 'detailInfo2', 'digest'))
        self.assertEqual(self.journal.db.execute('select sum(count) from calls').fetchone()[0], 1)

    def test_room_course_and_gallery_images_deduplicate_without_truncating(self):
        items = [{'originimgurl': 'https://example.com/a.jpg', 'imgname': '외관'}, {'roomimg1': 'https://example.com/a.jpg', 'roomimg5': 'https://example.com/b.jpg', 'subdetailimg': 'https://example.com/c.jpg'}, {'originimgurl': 'javascript:alert(1)'}]
        self.assertEqual({row['origin_image_url'] for row in image_rows(items)}, {'https://example.com/a.jpg', 'https://example.com/b.jpg', 'https://example.com/c.jpg'})


if __name__ == '__main__':
    unittest.main()
