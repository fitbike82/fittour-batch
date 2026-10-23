import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from scripts import sync_tour_enrichment_full as batch
from test_tour_enrichment_full import Response, payload


class OperationBudgetTests(unittest.TestCase):
    def test_image_and_menu_share_budget_persist_and_reset_next_day(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / 'cache.db')
            with patch.object(batch, 'kst_today', return_value='2026-10-21'):
                journal = batch.Journal(path, 'key')
                journal.endpoint_limit = 2
                journal.reserve('KorService2', 'detailImage2')
                journal.reserve('KorService2', 'detailImage2.menu')
                with self.assertRaises(batch.EndpointBudgetStop):
                    journal.reserve('KorService2', 'detailImage2')
                journal.reserve('KorService2', 'detailCommon2')
                journal.block('KorWithService2', 'detailImage2', '22')
                journal.db.close()
                resumed = batch.Journal(path, 'key')
                resumed.endpoint_limit = 2
                with self.assertRaises(batch.EndpointBudgetStop):
                    resumed.reserve('KorService2', 'detailImage2.menu')
                self.assertFalse(resumed.available('KorWithService2', 'detailImage2.menu'))
            with patch.object(batch, 'kst_today', return_value='2026-10-22'):
                resumed.reserve('KorService2', 'detailImage2')
                self.assertTrue(resumed.available('KorWithService2', 'detailImage2'))
                images = [r for r in resumed.endpoint_usage() if r['image']]
                self.assertEqual(sum(r['calls'] for r in images), 1)
            resumed.db.close()

    def test_image_provider_quota_does_not_block_other_apis_and_next_day_backfills_only_images(self):
        with tempfile.TemporaryDirectory() as d:
            places = [dict(content_id=str(i), content_type_id='12', modified_time='v1') for i in range(3)]
            stored = set()
            db = Mock()
            db.completed.side_effect = lambda: stored.copy()
            db.targets.return_value = places
            def rpc(name, data):
                if name == 'upsert_tour_enrichment':
                    stored.add((data['p_place_id'], data['p_service'], data['p_endpoint'], data['p_revision']))
                return True
            db.rpc.side_effect = rpc
            network = []
            day = ['2026-10-21']
            class Http:
                def get(self, url, **kwargs):
                    network.append((day[0], url.split('/')[-2], url.split('/')[-1]))
                    if day[0] == '2026-10-21' and url.endswith('/detailImage2'):
                        return Response({'OpenAPI_ServiceResponse': {'cmmMsgHeader': {'returnReasonCode': '22'}}}, 429)
                    return Response(payload([]))
            original_client = batch.TourClient
            def make_client(*args, **kwargs):
                client = original_client(*args, **kwargs)
                client.http = Http()
                return client
            def run():
                report, context = {}, {}
                batch._run_batch(['--checkpoint', str(Path(d) / 'cache.db'), '--delay', '0'], report, context)
                context['journal'].db.close()
                return report
            with patch.object(batch, 'Database', return_value=db), patch.object(batch, 'TourClient', side_effect=make_client), \
                 patch.object(batch, 'kst_today', side_effect=lambda: day[0]), patch.object(batch, 'load_dotenv'), \
                 patch.dict(os.environ, {'TOUR_API_KEY':'key','SUPABASE_URL':'url','SUPABASE_KEY':'key'}), \
                 contextlib.redirect_stdout(io.StringIO()):
                first = run()
                self.assertEqual(first['partial_saved_count'], 3)
                self.assertEqual(first['processed_count'], 0)
                self.assertEqual(first['non_image_complete_count'], 3)
                self.assertEqual(len(network), 26)  # 8 non-image operations x 3 + 2 quota replies.
                same_day = run()
                self.assertEqual(len(network), 26)  # Persisted blocks, no repeated calls.
                self.assertEqual(same_day['endpoints_saved'], 0)
                day[0] = '2026-10-22'
                next_day = run()
                self.assertEqual(next_day['processed_count'], 3)
                self.assertEqual(next_day['partial_saved_count'], 0)
                self.assertEqual(len(network), 32)
                self.assertTrue(all(endpoint == 'detailImage2' for date, service, endpoint in network if date == day[0]))

    def test_restaurant_full_completion_includes_menu_for_both_services(self):
        p = dict(content_id='1', content_type_id='39', modified_time='v1')
        rows = {('1', s, e, 'v1') for s, endpoints in batch.SERVICES.items() for e in endpoints}
        self.assertEqual(batch.enriched_count(rows, [p]), 0)
        rows.update(('1', s, 'detailImage2.menu', 'v1') for s in batch.SERVICES)
        self.assertEqual(batch.enriched_count(rows, [p]), 1)

    def test_http_429_code_23_is_retried_not_daily_blocked(self):
        from test_tour_enrichment_full import Http
        with tempfile.TemporaryDirectory() as d, patch.object(batch.time, 'sleep'):
            journal = batch.Journal(str(Path(d) / 'cache.db'), 'key')
            client = batch.TourClient('key', journal, delay=0)
            client.http = Http([Response({'OpenAPI_ServiceResponse': {'cmmMsgHeader': {'returnReasonCode': '23'}}}, 429), Response(payload([]))])
            client.collect('KorService2', 'detailCommon2', dict(content_id='1', content_type_id='12'))
            self.assertEqual(journal.day_calls()['KorService2'], 2)
            journal.db.close()
