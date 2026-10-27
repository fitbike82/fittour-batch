import contextlib
import io
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from scripts.gemini_key_pool import KeyPool, discover_keys, batch_slot, mask_keys
from pipeline_task2 import Gemini, GeminiDailyQuotaExhausted, GeminiRequestFailed, mask_sensitive

KEYS = {f"GEMINI_API_KEY_{i:02}": f"AIzaTestKeyNumber{i}abcdefghijklmnopqrstuvwxyz" for i in range(1,4)}

class KeyPoolTests(unittest.TestCase):
    def test_discovery_priority_dedup_and_legacy(self):
        self.assertEqual([x[0] for x in discover_keys(KEYS)], ['Key-01','Key-02','Key-03'])
        self.assertEqual(discover_keys({'GEMINI_API_KEY':'old'}), [('Key-legacy','old')])
        self.assertEqual(len(discover_keys({**KEYS,'GEMINI_API_KEY':'old'})),3)
        self.assertEqual(len(discover_keys({'GEMINI_API_KEY_01':'same','GEMINI_API_KEY_02':'same'})),1)

    def test_twenty_batches_have_7_7_6_distribution(self):
        labels=[KeyPool({**KEYS,'GEMINI_BATCH_SLOT':str(i)}).current()[0] for i in range(1,21)]
        self.assertEqual([labels.count(f'Key-{i:02}') for i in range(1,4)],[7,7,6])

    def test_actual_scheduled_hours_follow_reset_in_summer_and_winter(self):
        for month,reset_hour in ((7,7),(12,8)):
            start=datetime(2026,month,10,reset_hour,tzinfo=timezone.utc)
            from datetime import timedelta
            runs=[start+timedelta(hours=i) for i in range(24) if (start+timedelta(hours=i)).hour not in (17,18,19,20)]
            self.assertEqual([batch_slot(t) for t in runs],list(range(1,21)))

    def test_sparse_pool_and_all_exhausted(self):
        p=KeyPool({'GEMINI_API_KEY_03':'third','GEMINI_BATCH_SLOT':'1'})
        self.assertEqual(p.current(),('Key-03','third'));p.exhaust();self.assertIsNone(p.current())
        with self.assertRaises(ValueError):KeyPool({})
        with self.assertRaises(ValueError):KeyPool({**KEYS,'GEMINI_BATCH_SLOT':'21'})

    def client(self,responses):
        session=Mock();session.post.side_effect=responses
        with patch.dict('os.environ',{**KEYS,'GEMINI_BATCH_SLOT':'1'},clear=True):
            model=Gemini(session,sleep=Mock(),clock=lambda:0,max_rpm=100)
        return model

    def test_429_immediately_switches_and_never_reuses_blocked_key(self):
        m=self.client([Mock(status_code=429),Mock(status_code=200),Mock(status_code=200)])
        with contextlib.redirect_stdout(io.StringIO()):
            m.request('model',{});m.request('model',{})
        keys=[c.kwargs['headers']['x-goog-api-key'] for c in m.session.post.call_args_list]
        self.assertEqual(keys,[KEYS['GEMINI_API_KEY_01'],KEYS['GEMINI_API_KEY_02'],KEYS['GEMINI_API_KEY_02']])
        m.sleep.assert_not_called()
        self.assertEqual(m.request_events[0]['key_id'],'Key-01')
        self.assertEqual(m.request_events[1]['key_id'],'Key-02')

    def test_all_three_quota_failures_stop_without_looping(self):
        m=self.client([Mock(status_code=429)]*3)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(GeminiDailyQuotaExhausted):m.request('model',{})
        self.assertEqual(m.session.post.call_count,3)
        with self.assertRaises(GeminiDailyQuotaExhausted):m.request('model',{})
        self.assertEqual(m.session.post.call_count,3)

    def test_resource_exhausted_exception_switches(self):
        from google.api_core.exceptions import ResourceExhausted
        m=self.client([ResourceExhausted('quota'),Mock(status_code=200)])
        with contextlib.redirect_stdout(io.StringIO()):m.request('model',{})
        self.assertEqual(m.session.post.call_count,2)

    def test_nonquota_error_does_not_burn_other_keys(self):
        m=self.client([Mock(status_code=400)])
        with contextlib.redirect_stdout(io.StringIO()),self.assertRaises(GeminiRequestFailed):m.request('model',{})
        self.assertEqual(m.session.post.call_count,1);self.assertFalse(m.pool.blocked)

    def test_log_redaction_all_keys_and_unknown_google_keys(self):
        with patch.dict('os.environ',KEYS,clear=True):
            text=' '.join(KEYS.values())
            for fn in (mask_keys,mask_sensitive):
                masked=fn(text)
                for key in KEYS.values():self.assertNotIn(key,masked)
        self.assertNotIn(KEYS['GEMINI_API_KEY_01'],mask_keys(KEYS['GEMINI_API_KEY_01'],{}))

class PoolBatchOutcomeTests(unittest.TestCase):
    def test_exhausted_pool_releases_claims_without_consuming_attempts(self):
        from tests.test_review_quota import ready
        from batch_ai_summary_cron import run
        _,reviews=ready()
        session=Mock();session.post.side_effect=[Mock(status_code=429)]*3
        with patch.dict('os.environ',{**KEYS,'GEMINI_BATCH_SLOT':'1'},clear=True):
            model=Gemini(session,sleep=Mock(),clock=lambda:0,max_rpm=100)
        places=[{'content_id':str(i),'title':'park','claim_token':'token','attempt_number':1} for i in range(3)]
        db=Mock()
        with patch('batch_ai_summary_cron.pending_places',return_value=places), \
             patch('batch_ai_summary_cron.search_reviews',return_value=reviews), \
             patch('batch_ai_summary_cron.persist') as persist,contextlib.redirect_stdout(io.StringIO()):
            counts=run(db,Mock(),model,'key',3,sleep=Mock())
        self.assertEqual(session.post.call_count,3)
        self.assertEqual(counts['stop_reason'],'daily_quota_exceeded')
        self.assertTrue(counts['quota_stopped'])
        self.assertEqual(counts['errors'],0)
        persist.assert_not_called()
        db.rpc.assert_called_once_with('release_ai_review_work',{'claim_token':'token'})

    def test_workflow_verifier_accepts_quota_stop_but_rejects_real_errors(self):
        import importlib.util
        from pathlib import Path
        spec=importlib.util.spec_from_file_location('review_verifier',Path('scripts/verify_review_report.py'))
        verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(verifier)
        import json
        for errors in (0,1):
            report={'quota_stopped':True,'stop_reason':'daily_quota_exceeded','quota_deferred':1,'errors':errors}
            with patch('sys.argv',['verify','--allow-quota-stop']), \
                 patch.object(Path,'is_file',return_value=True), \
                 patch.object(Path,'read_text',return_value=json.dumps(report)),contextlib.redirect_stdout(io.StringIO()):
                if errors:
                    with self.assertRaises(SystemExit):verifier.main()
                else:verifier.main()


class DailyPersistenceTests(unittest.TestCase):
    def test_daily_exhaustion_survives_processes_and_resets_next_provider_day(self):
        import tempfile,json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json';env={**KEYS,'GEMINI_BATCH_SLOT':'1','GEMINI_KEY_POOL_STATE':str(path)}
            now=datetime(2026,10,27,9,tzinfo=timezone.utc)
            p=KeyPool(env,now);p.exhaust(daily=True)
            self.assertEqual(KeyPool(env,now).current()[0],'Key-02')
            nextday=datetime(2026,10,28,9,tzinfo=timezone.utc)
            self.assertEqual(KeyPool(env,nextday).current()[0],'Key-01')
            for secret in KEYS.values():self.assertNotIn(secret,path.read_text())
            rotated={**env,'GEMINI_API_KEY_01':'new-project-key'}
            self.assertEqual(KeyPool(rotated,now).current()[0],'Key-01')

    def test_minute_quota_is_not_persisted_as_daily_exhaustion(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json';env={**KEYS,'GEMINI_BATCH_SLOT':'1','GEMINI_KEY_POOL_STATE':str(path)}
            p=KeyPool(env);p.exhaust();self.assertEqual(p.current()[0],'Key-02')
            self.assertEqual(KeyPool(env).current()[0],'Key-01');self.assertFalse(path.exists())

    def test_daily_quota_body_switches_even_when_transport_status_is_not_429(self):
        response=Mock(status_code=403,json=lambda:{'error':{'status':'RESOURCE_EXHAUSTED','message':'Daily_quota_Exceeded'}})
        session=Mock();session.post.side_effect=[response,Mock(status_code=200)]
        with patch.dict('os.environ',{**KEYS,'GEMINI_BATCH_SLOT':'1'},clear=True):model=Gemini(session,sleep=Mock(),clock=lambda:0,max_rpm=100)
        with contextlib.redirect_stdout(io.StringIO()):model.request('model',{})
        self.assertEqual(model.pool.daily_blocked,{0});self.assertEqual(session.post.call_count,2)

    def test_key_redaction_does_not_reveal_suffix(self):
        masked=mask_keys(' '.join(KEYS.values()),KEYS)
        self.assertEqual(masked,'AIzaSy**** AIzaSy**** AIzaSy****')

if __name__=='__main__':unittest.main()
