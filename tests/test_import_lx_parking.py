import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import import_lx_parking as lx


class LXImportTests(unittest.TestCase):
    def test_floor_suffix_cleanup_and_utf8_input(self):
        self.assertIn('서울특별시 중랑구 망우로 307', lx.address_queries('서울특별시 중랑구 망우로 3073층(상봉동)'))
        self.assertIn('인천광역시 남동구 인하로 545', lx.address_queries('인천광역시 남동구 인하로 545407호'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'utf8.csv'
            path.write_text('부서명,주소,주차장 이용료,주차장 운영시간\n서울,서울특별시,무료,09:00~18:00\n', encoding='utf-8-sig')
            rows, encoding = lx.read_lx(path)
            self.assertEqual(encoding, 'utf-8-sig')
            self.assertEqual(rows[0]['주소'], '서울특별시')

    def test_cp949_fixture_and_idempotent_normalization(self):
        # Standalone CI must not depend on publishing the original source dataset.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic-lx.csv'
            with path.open('w', encoding='cp949', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=['부서명', '주소', '주차장 이용료', '주차장 운영시간'])
                writer.writeheader()
                for index in range(179):
                    writer.writerow({'부서명': f'테스트지사{index}', '주소': f'서울특별시 중구 시험로 {index + 1}', '주차장 이용료': '무료', '주차장 운영시간': '09:00~18:00'})
            rows, encoding = lx.read_lx(path)
        self.assertEqual(encoding, 'cp949')
        self.assertEqual(len(rows), 179)
        record = lx.normalize(rows[0], (37.5, 127.1))
        self.assertEqual(record['parking_no'], lx.normalize(rows[0], (37.6, 127.2))['parking_no'])
        self.assertTrue(record['is_free'])
        self.assertEqual(record['latitude'], 37.5)
        self.assertEqual(record['longitude'], 127.1)
        self.assertIn('2023-08-29', record['discount_info'])
        paid = {**rows[0], '주차장 이용료': '유료', '주차장 운영시간': ''}
        self.assertFalse(lx.normalize(paid, (37.5, 127))['is_free'])
        self.assertIsNone(lx.normalize(paid, (37.5, 127))['basic_fee'])

    @patch.object(lx.time, 'sleep')
    @patch.object(lx, 'urlopen')
    def test_xy_orientation_and_durable_cache(self, http, sleep):
        with tempfile.TemporaryDirectory() as directory:
            http.side_effect = lambda *a, **k: io.BytesIO(json.dumps({'documents': [{'x': '127.1', 'y': '37.5'}]}).encode())
            geocoder = lx.Geocoder('secret', Path(directory) / 'cache.db')
            self.assertEqual(geocoder.lookup('주소'), (37.5, 127.1))
            geocoder.cache.close()
            geocoder = lx.Geocoder('secret', Path(directory) / 'cache.db')
            try:
                self.assertEqual(geocoder.lookup('주소'), (37.5, 127.1))
                self.assertEqual(http.call_count, 1)
            finally:
                geocoder.cache.close()

    @patch.object(lx.time, 'sleep')
    @patch.object(lx, 'urlopen')
    def test_empty_address_and_quota_failure_are_not_fake_coordinates(self, http, sleep):
        with tempfile.TemporaryDirectory() as directory:
            geocoder = lx.Geocoder('secret', Path(directory) / 'cache.db')
            try:
                self.assertIsNone(geocoder.lookup(''))
                http.assert_not_called()
                http.side_effect = HTTPError('private-url', 429, 'quota', None, None)
                with self.assertRaisesRegex(RuntimeError, 'Kakao HTTP 429'):
                    geocoder.lookup('주소')
                self.assertEqual(geocoder.cache.execute('select count(*) from geocodes').fetchone()[0], 0)
            finally:
                geocoder.cache.close()
