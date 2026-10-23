import csv
import importlib.util
import io
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('parking_import', Path(__file__).resolve().parents[1] / 'scripts/import_parking_lots.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ParkingImportTests(unittest.TestCase):
    def test_coordinates_duplicates_fees_and_discount(self):
        fields = ['주차장관리번호', '주차장명', '위도', '경도', '요금정보', '주차기본요금', '특기사항', '데이터기준일자']
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader()
        def row(identifier, lat='37.5', date='2026-09-01', fee='1,000'):
            writer.writerow(dict(zip(fields, [identifier, '테스트', lat, '127', '무료', fee, '경차 50% 할인', date])))
        row('a'); row('a', date='2025-01-01', fee='900'); row('zero', lat='0'); row('nan', lat='nan'); row('outside', lat='40')
        rows, stats = module.parse_rows(buffer.getvalue())
        self.assertEqual(len(rows), 1)
        self.assertEqual(stats['invalid_coordinates'], 3)
        self.assertEqual(stats['duplicate_ids'], 1)
        self.assertEqual(rows[0]['basic_fee'], 1000)
        self.assertTrue(rows[0]['is_free'])
        self.assertEqual(rows[0]['discount_info'], '경차 50% 할인')
        self.assertEqual(module.number('-'), 0)
        self.assertIsNone(module.number('', None))


if __name__ == '__main__':
    unittest.main()
