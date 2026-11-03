import json
import unittest
from unittest.mock import Mock

from scripts.place_taxonomy import classify, validate_taxonomy, model_schema, extract_filter_tags, resolve_food_category
from scripts.import_century_stores import match_records, update_payload, address_key
from pipeline_task2 import Gemini


class PlaceTaxonomyTests(unittest.TestCase):
    def place(self, **kwargs):
        return {'content_id': '1', 'content_type_id': '39', 'title': '서울갈비',
                'address': '서울특별시 종로구 삼일대로 8-5', 'tel': '02-1234-5678',
                'is_active': True, **kwargs}

    def record(self, **kwargs):
        return {'연번': '1', '업체명': '서울갈비', '기본주소': '서울 종로구 삼일대로 8-5',
                '연락처': '02-1234-5678', '시도': '서울', '시군구': '종로구', '주요사업': '갈비, 냉면', **kwargs}

    def test_common_config_multiple_tags_and_nullable_l2(self):
        result = classify(self.place(), '갈비, 냉면')
        self.assertEqual(result['category_l1'], '음식점')
        self.assertEqual(result['category_l2'], '냉면/밀면')
        self.assertEqual(result['filter_tags'], ['갈비', '냉면'])
        self.assertIsNone(classify(self.place(title='모호한상호'))['category_l2'])
        self.assertEqual(classify(self.place(cat1='A01'), '냉면')['category_l1'], '음식점')

    def test_invalid_model_taxonomy_and_certification_tag_are_rejected(self):
        for data in [dict(category_l1='쇼핑', category_l2=None, filter_tags=[]),
                     dict(category_l1='음식점', category_l2='호텔', filter_tags=[]),
                     dict(category_l1='음식점', category_l2=None, filter_tags=['백년가게']),
                     dict(category_l1='음식점', category_l2=None, filter_tags=['주차가능'])]:
            with self.assertRaises(ValueError):
                validate_taxonomy(data, self.place())

    def test_name_only_and_cross_region_phone_do_not_auto_match(self):
        rows = match_records([self.record(연락처='')], [self.place(address='서울 종로구 다른길 99')])
        self.assertEqual(rows[0]['status'], 'review')
        rows = match_records([self.record()], [self.place(address='부산광역시 해운대구 삼일대로 8-5')])
        self.assertEqual(rows[0]['status'], 'review')

    def test_exact_match_collision_and_inactive_exclusion(self):
        self.assertEqual(match_records([self.record()], [self.place()])[0]['status'], 'matched')
        self.assertEqual(match_records([self.record()], [self.place(is_active=False)])[0]['status'], 'unmatched')
        rows = match_records([self.record(), self.record(연번='2')], [self.place()])
        self.assertTrue(all(row['status'] == 'review' for row in rows))
        self.assertNotEqual(address_key('서울 종로구 삼일대로 8-5'), address_key('서울 종로구 삼일대로 85'))

    def test_csv_preserves_manual_classification_and_tags(self):
        place = self.place(taxonomy_source='manual:v1', category_l1='음식점', category_l2='한식', filter_tags=['냉면'])
        payload = update_payload(self.record(), place, 'hash', {'reason': 'name_phone_region'})
        self.assertNotIn('category_l1', payload)
        self.assertNotIn('taxonomy_source', payload)
        self.assertEqual(payload['filter_tags'], ['냉면', '백년가게'])
        self.assertFalse(payload['century_store_info']['current_certification_verified'])

    def test_gemini_uses_dynamic_schema_and_returns_validated_taxonomy(self):
        response = {'quality_status': 'ready', 'summary_lines': ['요약1', '요약2', '요약3'],
                    'pros': [], 'cons': [], 'visit_tips': '확인하세요.',
                    'category_l1': '음식점', 'category_l2': '냉면/밀면', 'filter_tags': ['갈비', '냉면', '갈비']}
        session = Mock()
        session.post.return_value = Mock(status_code=200, json=lambda: {
            'candidates': [{'content': {'parts': [{'text': json.dumps(response)}]}}]})
        generator = Gemini(session, 'test-key', min_interval=0, max_rpm=100)
        result, _ = generator.summarize(self.place(cat1='DO_NOT_SEND'), [{'source_id': 1, 'text': '갈비와 냉면을 판매하는 식당입니다.'}])
        self.assertEqual(result['filter_tags'], ['갈비', '냉면'])
        schema = session.post.call_args.kwargs['json']['generationConfig']['responseJsonSchema']
        self.assertEqual(schema['properties']['category_l1']['enum'], ['음식점', '카페/디저트'])
        self.assertNotIn('enum', schema['properties']['filter_tags']['items'])
        self.assertEqual(schema['properties']['filter_tags']['maxItems'], 12)
        self.assertIn('filter_tags 허용값', session.post.call_args.kwargs['json']['systemInstruction']['parts'][0]['text'])
        self.assertEqual(model_schema(self.place())['category_l2']['nullable'], True)
        self.assertNotIn('DO_NOT_SEND', json.dumps(session.post.call_args.kwargs['json']))

    def test_invalid_taxonomy_cannot_bypass_correction_via_summary_salvage(self):
        response = {'quality_status': 'ready', 'summary_lines': ['요약1', '요약2', '요약3'],
                    'pros': [], 'cons': [], 'visit_tips': '확인하세요.',
                    'category_l1': '숙박', 'category_l2': None, 'filter_tags': []}
        session = Mock()
        session.post.return_value = Mock(status_code=200, json=lambda: {
            'candidates': [{'content': {'parts': [{'text': json.dumps(response)}]}}]})
        generator = Gemini(session, 'test-key', min_interval=0, max_rpm=100)
        with self.assertRaisesRegex(ValueError, 'Invalid taxonomy'):
            generator.summarize(self.place(), [{'source_id': 1, 'text': '갈비와 냉면을 판매하는 식당입니다.'}])

    def test_pm_v2_dessert_branch_business_priority_and_normalization(self):
        self.assertEqual(resolve_food_category('떡볶이'), {'l1': '음식점', 'l2': '만두/분식'})
        self.assertEqual(resolve_food_category('떡갈비')['l1'], '음식점')
        self.assertEqual(resolve_food_category('찹쌀떡')['l1'], '카페/디저트')
        self.assertEqual(classify(self.place(title='갈비카페'), '돈가스')['category_l2'], '일식/돈까스')
        self.assertEqual(extract_filter_tags('돈가스/돈까스, 까페·카페 (전문점) 등 떡'), ['돈까스', '카페', '떡'])
        self.assertEqual(model_schema(self.place(content_type_id='38'))['category_l2'].get('enum'), None)
        self.assertEqual(validate_taxonomy({'category_l1': '카페/디저트', 'category_l2': '카페/음료',
                                            'filter_tags': ['까페']}, self.place())['filter_tags'], ['카페'])
        with self.assertRaises(ValueError):
            validate_taxonomy({'category_l1': '카페/디저트', 'category_l2': None, 'filter_tags': []}, self.place())

