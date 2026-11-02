import json
from pathlib import Path
import tempfile
import unittest

from scripts.resolve_century_reviews import clean_title, numbers, candidate_evidence, rule4_pass, resolve_records, merge_unmatched


class RelaxedCenturyTests(unittest.TestCase):
    def record(self, **kwargs):
        return {'연번': '1', '업체명': '미세스고등어본점', '기본주소': '경북 포항시 남구 오천읍 원리 873',
                '연락처': '054-111-1234', '시도': '경북', '시군구': '포항시 남구', '주요사업': '생선구이', **kwargs}

    def place(self, **kwargs):
        return {'content_id': 'poi1', 'content_type_id': '39', 'title': '미세스고등어',
                'address': '경상북도 포항시 남구 오천읍 다른길 12', 'tel': '054-999-1234',
                'is_active': True, **kwargs}

    def original(self, *ids):
        return {'events': [{'csv_id': '1', 'csv_name': '미세스고등어본점', 'status': 'review',
                            'candidates': [{'content_id': content_id} for content_id in ids]}]}

    def test_rule1_corporate_suffix_and_region(self):
        self.assertEqual(clean_title('(주) 미세스고등어 본점 직영점'), '미세스고등어')
        candidate = candidate_evidence(self.record(), self.place())
        self.assertIn('rule_1_core_name_region', candidate['rules'])
        self.assertTrue(candidate['region_match'])

    def test_rule2_substring_and_four_digit_suffix(self):
        candidate = candidate_evidence(self.record(), self.place(title='미세스고등어남구점'))
        self.assertIn('rule_2_substring_phone_suffix', candidate['rules'])
        candidate = candidate_evidence(self.record(), self.place(title='미세스고등어남구점', tel='054-999-9999'))
        self.assertNotIn('rule_2_substring_phone_suffix', candidate['rules'])

    def test_rule3_contiguous_core_and_cross_address_number_preserves_lot_suffix(self):
        candidate = candidate_evidence(self.record(), self.place(title='미세스고등어식당', address='경북 포항시 남구 새도로 873'))
        self.assertIn('rule_3_name_building_number', candidate['rules'])
        self.assertEqual(numbers('서울 종로구 새로780번길 35 2층'), {'35'})
        self.assertEqual(numbers('서울 종로구 원동 873-1'), {'873-1'})
        self.assertNotIn('873', numbers('서울 종로구 원동 873-1'))

    def test_rule4_boundary_and_explicit_no_region_requirement(self):
        self.assertTrue(rule4_pass(.80, 5))
        self.assertTrue(rule4_pass(.80, None))
        self.assertFalse(rule4_pass(.799, 20))
        self.assertFalse(rule4_pass(.90, 4))
        result = resolve_records(self.original('poi1'), [self.record()],
                                 [self.place(address='서울특별시 종로구 새로 1', tel=None)])[0]
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['matched_rules'], ['rule_4_similarity_margin'])
        self.assertFalse(result['region_match'])

    def test_ambiguous_candidates_and_other_csv_reservation_are_rejected(self):
        candidates = [self.place(), self.place(content_id='poi2')]
        result = resolve_records(self.original('poi1', 'poi2'), [self.record()], candidates)[0]
        self.assertEqual(result['reason'], 'ambiguous_equal_candidates')
        original = self.original('poi1')
        original['events'].append({'csv_id': 'old', 'status': 'matched', 'content_id': 'poi1'})
        self.assertEqual(resolve_records(original, [self.record()], [self.place()])[0]['reason'], 'already_matched_to_other_csv')
        self.assertEqual(resolve_records(self.original('poi1'), [self.record()], [self.place(is_active=False)])[0]['status'], 'rejected')

    def test_unmatched_queue_is_deduplicated_and_removes_only_confirmed_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'queue.json'
            path.write_text(json.dumps([{'csv_id': 'preserved'}, {'csv_id': '1'}]), encoding='utf-8')
            original = {'events': [{'csv_id': '2', 'status': 'unmatched'}]}
            records = [self.record(), self.record(연번='2')]
            events = [{'csv_id': '1', 'status': 'accepted', 'reason': 'rule1', 'candidates': [], 'committed': True}]
            self.assertEqual(merge_unmatched(path, original, events, records), 2)
            self.assertEqual(merge_unmatched(path, original, events, records), 2)
            ids = {row['csv_id'] for row in json.loads(path.read_text(encoding='utf-8'))['records']}
            self.assertEqual(ids, {'preserved', '2'})
