import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.gemini_key_pool import KeyPool
from pipeline_task2 import Gemini, GeminiRequestFailed, api_error_details, json_response_schema


class RecoveryContractTests(unittest.TestCase):
    def test_ttl_releases_daily_state_and_live_process_crosses_provider_midnight(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            env = {'GEMINI_API_KEY': 'secret', 'GEMINI_KEY_POOL_STATE': str(path)}
            now = datetime(2026, 11, 3, 3, tzinfo=timezone.utc)
            pool = KeyPool(env, now)
            pool.exhaust(daily=True)
            self.assertIsNone(pool.current())
            pool.now = lambda: now + timedelta(hours=4)
            self.assertEqual(pool.current(), ('Key-legacy', 'secret'))
            self.assertEqual(json.loads(path.read_text())['exhausted'], [])
            pool.exhaust(daily=True)
            pool.now = lambda: now + timedelta(hours=6)  # Pacific midnight was 08 UTC.
            self.assertIsNotNone(pool.current())
            self.assertEqual(pool.day, '2026-11-03')

    def test_legacy_state_has_finite_ttl_and_malformed_timestamp_is_ignored(self):
        import hashlib, os
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            env = {'GEMINI_API_KEY': 'secret', 'GEMINI_KEY_POOL_STATE': str(path)}
            now = datetime(2026, 11, 3, 3, tzinfo=timezone.utc)
            path.write_text(json.dumps({'version': 1, 'day': '2026-11-02',
                'exhausted': [hashlib.sha256(b'secret').hexdigest()]}))
            os.utime(path, (now.timestamp()-14401, now.timestamp()-14401))
            self.assertIsNotNone(KeyPool(env, now).current())
            for value in ('nan', 'inf', now.timestamp()+1000):
                path.write_text(json.dumps({'version': 2, 'day': '2026-11-02',
                    'exhausted': [hashlib.sha256(b'secret').hexdigest()],
                    'blocked_at': {hashlib.sha256(b'secret').hexdigest(): value}}))
                self.assertIsNotNone(KeyPool(env, now).current())

    def test_bad_request_preserves_bounded_masked_provider_message(self):
        secret = 'AIzaTestKeyNumber1abcdefghijklmnopqrstuvwxyz'
        response = Mock(status_code=400, json=lambda: {'error': {'status': 'INVALID_ARGUMENT',
            'message': 'Schema invalid '+secret, 'code': 400}})
        with patch.dict('os.environ', {'GEMINI_API_KEY': secret}, clear=True):
            client = Gemini(Mock(post=Mock(return_value=response)), key=secret)
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(GeminiRequestFailed):
                client.request('model', {})
        event = client.request_events[0]['provider_error']
        self.assertEqual(event['status'], 'INVALID_ARGUMENT')
        self.assertNotIn(secret, json.dumps(event))
        self.assertIn('Schema invalid', event['message'])

    def test_json_schema_nullable_enum_is_valid_and_does_not_mutate_source(self):
        source = {'type': 'STRING', 'nullable': True, 'enum': ['x']}
        self.assertEqual(json_response_schema(source), {'type': ['string', 'null'], 'enum': ['x', None]})
        self.assertTrue(source['nullable'])

    def test_nonquota_failure_resets_consecutive_429_streak(self):
        client = Gemini(Mock(post=Mock(return_value=Mock(status_code=400))), key='secret')
        client.consecutive_429 = 19
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(GeminiRequestFailed):
            client.request('model', {})
        self.assertEqual(client.consecutive_429, 0)


if __name__ == '__main__':
    unittest.main()
