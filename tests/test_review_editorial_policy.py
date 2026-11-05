import unittest

from pipeline_task2 import validate_summary
from scripts.review_evidence import resolve_points


class EditorialPolicyTests(unittest.TestCase):
    def candidate(self):
        return {'quality_status': 'ready', 'summary_lines': ['위치 설명', '시설 설명', '이용 설명'],
                'pros': [], 'cons': [], 'visit_tips': ''}

    def test_rejects_service_problems_in_all_public_fields(self):
        for field, text in (
            ('summary_lines', '일부 리뷰에는 다른 지점 내용이 혼재되어 있습니다.'),
            ('cons', '일부 지점은 대중교통 이용이 불편해 차량 방문이 권장됨'),
            ('visit_tips', '방문하시는 지점의 정확한 위치와 주차 환경을 미리 확인하세요.'),
            ('visit_tips', '공식 페이지를 통해 가격표를 확인하세요.'),
        ):
            with self.subTest(field=field):
                data = self.candidate()
                if field == 'summary_lines':
                    data[field][2] = text
                elif field == 'cons':
                    data[field] = [{'text': text, 'source_id': 1, 'evidence': text}]
                else:
                    data[field] = text
                with self.assertRaisesRegex(ValueError, 'Invalid service-data'):
                    validate_summary(data, [{'source_id': 1, 'text': text}])

    def test_empty_tip_and_evidence_backed_detail_candidate(self):
        data = self.candidate()
        quote = '뷔페 이용 시 세 시간 무료 주차를 지원합니다.'
        data['detail_candidates'] = [{'topic': 'parking', 'text': '뷔페 이용객 무료 주차',
                                      'evidence_id': 'parking-1'}]
        resolve_points(data, {'parking-1': {'source_id': 1, 'evidence': quote}}, [])
        self.assertEqual(validate_summary(data, [{'source_id': 1, 'text': quote}]), data)
        data['detail_candidates'][0]['evidence'] = '실제 원문에는 존재하지 않는 상세 정보입니다.'
        with self.assertRaises(ValueError):
            validate_summary(data, [{'source_id': 1, 'text': quote}])

    def test_separate_fields_do_not_form_a_forbidden_sentence(self):
        data = self.candidate()
        data['summary_lines'] = ['시설 정보를 소개합니다.', '주차 공간이 부족합니다.', '관람 동선을 소개합니다.']
        self.assertEqual(validate_summary(data, []), data)
