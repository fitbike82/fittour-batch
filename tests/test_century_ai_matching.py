from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.century_ai_matching import (isFoodBusiness, prepare, top_candidates, MinuteGate,
    parse_result, run_batch, state_lock, GeminiQuotaDeferred, atomic_json, MatchingSession, final_summary, seed_digest)


class CenturyAIMatchingTests(unittest.TestCase):
    def test_seed_digest_survives_windows_to_linux_checkout_but_detects_data_changes(self):
        self.assertEqual(seed_digest(b'{\r\n"records": []\r\n}'), seed_digest(b'{\n"records": []\n}'))
        self.assertNotEqual(seed_digest(b'{"records": []}'), seed_digest(b'{"records": [1]}'))

    def test_final_summary_reports_remaining_then_verified_completion(self):
        state = {'seed_hash': 'hash', 'deployment_pending_ids': ['1'], 'jobs': [
            {'csv_id': '1', 'status': 'pending'}, {'csv_id': '2', 'status': 'no_match'}]}
        db = Mock()
        db.table.return_value.select.return_value.eq.return_value.range.return_value.execute.return_value.count = 325
        result = final_summary(state, db)
        self.assertFalse(result['completed'])
        self.assertEqual(result['remaining'], 1)
        state['jobs'][0].update(status='matched', content_id='poi1')
        query = db.table.return_value.select.return_value.in_.return_value.execute.return_value
        query.data = [{'content_id': 'poi1', 'is_century_store': True, 'century_store_info': {'csv_id': '1'}}]
        result = final_summary(state, db)
        self.assertTrue(result['completed'])
        self.assertEqual(result['db_century_total'], 325)
        self.assertEqual(result['deployment_statuses'], {'matched': 1})
        query.data = []
        self.assertFalse(final_summary(state, db)['completed'])

    def record(self, **kwargs):
        return {'연번': '1', '업체명': '미세스고등어본점', '주요사업': '생선구이',
                '시도': '경북', '시군구': '포항시 남구', '기본주소': '경북 포항시 남구 원리 873',
                '연락처': '054-111-1234', **kwargs}

    def place(self, **kwargs):
        return {'content_id': 'poi1', 'content_type_id': '39', 'title': '미세스고등어',
                'address': '경북 포항시 남구 오천읍 새로 873', 'tel': '054-111-1234',
                'is_active': True, 'is_century_store': False, 'filter_tags': [], **kwargs}

    def response(self, index=0, confidence='NONE'):
        return Mock(json=lambda: {'candidates': [{'content': {'parts': [{'text': json.dumps({
            'matched_index': index, 'confidence': confidence, 'reason': '제공 자료 대조 결과'})}]}}]})

    def state(self, count):
        return {'seed_hash': 'hash', 'jobs': [{'csv_id': str(i), 'source_record': self.record(연번=str(i)),
            'status': 'pending', 'attempts': 0} for i in range(count)]}

    def test_pm_dictionaries_override_before_blacklist_without_extra_inference(self):
        for business in ('미용', '안경 판매', '의류', '자동차 정비', '방앗간', '', None):
            self.assertFalse(isFoodBusiness(business))
        for business in ('떡/방앗간', '제과', '카페 및 서적', '냉면', '갈비'):
            self.assertTrue(isFoodBusiness(business))
        # The supplied PM list has 쌀 but no 세탁. Follow it literally.
        self.assertFalse(isFoodBusiness('쌀국수'))
        self.assertTrue(isFoodBusiness('세탁'))

    def test_preparation_partitions_and_enforces_local_food_top_three(self):
        seed = {'records': [{'csv_id': str(i), 'source_record': record} for i, record in enumerate([
            self.record(주요사업='미용'), self.record(), self.record(업체명='완전다른곳', 시군구='경주시')])]}
        places = [self.place(content_id=str(i)) for i in range(6)] + [self.place(content_id='outside', address='서울 종로구 새로 873')]
        state, skipped, missing = prepare(seed, places)
        self.assertEqual((len(state['jobs']), len(skipped), len(missing)), (1, 1, 1))
        self.assertEqual(len(state['jobs'][0]['candidates']), 3)
        self.assertTrue(all(c['content_id'] != 'outside' for c in state['jobs'][0]['candidates']))
        self.assertEqual(top_candidates(self.record(), [self.place(is_century_store=True)]), [])

    def test_persisted_global_gate_controls_retries_and_rolling_windows(self):
        now, starts = [0.0], []
        def sleep(seconds):
            now[0] += seconds
        state = {}
        for _ in range(40):
            gate = MinuteGate(state, lambda: None, lambda: now[0], sleep) # Resume/key-switch uses persisted starts.
            gate.wait()
            starts.append(now[0])
        self.assertTrue(all(b-a >= 4.5 for a, b in zip(starts, starts[1:])))
        self.assertTrue(all(sum(start-60 < previous <= start for previous in starts) <= 15 for start in starts))
        state = {'request_times': list(range(15))}
        now[0] = 14
        MinuteGate(state, lambda: None, lambda: now[0], sleep).wait()
        self.assertGreaterEqual(now[0], 60)

    def test_schema_rejects_hallucinated_indexes_boolean_and_contradictions(self):
        candidates = top_candidates(self.record(), [self.place()])
        for response in (self.response(2, 'HIGH'), self.response(True, 'HIGH'), self.response(0, 'HIGH')):
            with self.assertRaises(ValueError):
                parse_result(response, candidates)

    def test_each_slot_fifty_and_all_slots_two_hundred_fifty_with_resume(self):
        state, generator = self.state(251), Mock(model='test-model')
        generator.request.return_value = self.response()
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        for slot in ('02:00', '04:00', '04:30', '05:30', '06:30'):
            result = run_batch(state, [self.place()], generator, lambda: None, Mock(), slot=slot, now=now)
            self.assertEqual(result['started'], 50)
            again = run_batch(state, [self.place()], generator, lambda: None, Mock(), slot=slot, now=now)
            self.assertEqual(again['started'], 0)
        self.assertEqual(generator.request.call_count, 250)
        self.assertEqual(sum(job['status'] == 'pending' for job in state['jobs']), 1)
        self.assertEqual(run_batch(state, [self.place()], generator, lambda: None, Mock(), slot='manual', now=now)['started'], 0)

    def test_medium_is_review_high_is_guarded_and_marks_other_job_unavailable(self):
        generator = Mock(model='test-model')
        generator.request.return_value = self.response(1, 'MEDIUM')
        state = self.state(1)
        with patch('scripts.century_ai_matching.conditional_update') as update:
            self.assertEqual(run_batch(state, [self.place()], generator, lambda: None, Mock())['review'], 1)
            update.assert_not_called()
        generator.request.return_value = self.response(1, 'HIGH')
        state = self.state(2)
        with patch('scripts.century_ai_matching.conditional_update', return_value=True) as update:
            report = run_batch(state, [self.place()], generator, lambda: None, Mock())
            self.assertEqual(report['matched'], 1)
            self.assertEqual(state['jobs'][1]['status'], 'no_candidate')
            self.assertIn('백년가게', update.call_args.args[2]['filter_tags'])

    def test_provider_quota_preserves_retry_and_deadline_stops_before_call(self):
        generator = Mock(model='test-model')
        generator.request.side_effect = GeminiQuotaDeferred('quota')
        state = self.state(2)
        report = run_batch(state, [self.place()], generator, lambda: None, Mock())
        self.assertTrue(report['deferred'])
        self.assertEqual(state['jobs'][0]['status'], 'retry')
        self.assertEqual(state['jobs'][1]['status'], 'pending')
        generator.reset_mock()
        report = run_batch(self.state(1), [self.place()], generator, lambda: None, Mock(), deadline=1, clock=lambda: 2)
        self.assertEqual(report['stop_reason'], 'time_budget')
        generator.request.assert_not_called()

    def test_local_state_lock_rejects_parallel_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            with state_lock(Path(directory)):
                with self.assertRaises(FileExistsError):
                    with state_lock(Path(directory)):
                        pass

    def test_checkpoint_replace_retries_permission_error_without_losing_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            original = Path.replace
            attempts = []
            def replace(source, target):
                attempts.append(1)
                if len(attempts) == 1:
                    raise PermissionError('temporary Windows lock')
                return original(source, target)
            with patch.object(Path, 'replace', replace), patch('scripts.century_ai_matching.time.sleep'):
                atomic_json(path, {'saved': True})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'saved': True})

    def test_429_sets_global_cooldown_before_key_switch(self):
        gate = Mock()
        response = Mock(status_code=429, headers={'Retry-After': '90'}, json=lambda: {'error': {}})
        with patch('requests.Session.post', return_value=response):
            MatchingSession(gate).post('https://example.invalid')
        gate.wait.assert_called_once()
        gate.cooldown.assert_called_once_with(90.0)
