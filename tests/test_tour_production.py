import contextlib
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from scripts import sync_tour_enrichment_full as batch
from scripts import sync_places_universe as catalog


class ProductionTourTests(unittest.TestCase):
    def test_production_boundary_and_independent_unapproved_service_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            journal=batch.Journal(str(Path(directory)/'state.db'),'key')
            self.assertEqual(journal.endpoint_limit,100000)
            day=journal.ensure_day()
            journal.db.execute('insert into operation_calls values(?,?,?,?,?)',(journal.key_id,day,'KorService2','detailImage2',99998));journal.db.commit()
            journal.reserve('KorService2','detailImage2');journal.reserve('KorService2','detailImage2.menu')
            with self.assertRaises(batch.EndpointBudgetStop):journal.reserve('KorService2','detailImage2')
            journal.reserve('KorService2','detailIntro2')
            journal.service_limits['KorWithService2']=1000
            row=next(x for x in journal.endpoint_usage() if x['service']=='KorWithService2')
            self.assertEqual(row['limit'],1000)
            journal.db.close()

    def test_approval_upgrade_reopens_only_once_without_resetting_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            journal=batch.Journal(str(Path(directory)/'state.db'),'key')
            journal.reserve('KorService2','detailImage2')
            journal.block('KorService2','detailImage2','22')
            journal.block('KorWithService2','detailImage2','22')
            self.assertEqual(journal.configure_policy('KorService2',100000)['reopened_blocks'],1)
            self.assertTrue(journal.available('KorService2','detailImage2'))
            self.assertFalse(journal.available('KorWithService2','detailImage2'))
            self.assertEqual(next(x['calls'] for x in journal.endpoint_usage() if x['service']=='KorService2' and x['endpoint']=='detailImage2'),1)
            journal.block('KorService2','detailImage2','22')
            self.assertEqual(journal.configure_policy('KorService2',100000)['reopened_blocks'],0)
            self.assertFalse(journal.available('KorService2','detailImage2'))
            journal.db.close()

    def test_parallel_reservations_never_overspend_shared_sqlite_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            journal=batch.Journal(str(Path(directory)/'state.db'),'key');journal.endpoint_limit=50
            def reserve(_):
                try:journal.reserve('KorService2','detailImage2');return 1
                except batch.EndpointBudgetStop:return 0
            with ThreadPoolExecutor(max_workers=8) as pool:self.assertEqual(sum(pool.map(reserve,range(80))),50)
            self.assertEqual(next(x['calls'] for x in journal.endpoint_usage() if x['service']=='KorService2' and x['endpoint']=='detailImage2'),50)
            journal.db.close()

    def test_four_worker_collection_retains_all_restaurant_menu_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Mock();db.completed.return_value=set();db.targets.return_value=[{'content_id':'1','content_type_id':'39','modified_time':'v1'}];db.rpc.return_value=True
            barrier=threading.Barrier(4);lock=threading.Lock();active=[0,0]
            def collect(client,service,endpoint,place):
                with lock:active[0]+=1;active[1]=max(active[1],active[0])
                barrier.wait(timeout=10)
                with lock:active[0]-=1
                return [{'response':{}}],[]
            report,context={},{}
            with patch.object(batch,'Database',return_value=db),patch.object(batch.TourClient,'collect',collect),patch.object(batch,'load_dotenv'),patch.dict(os.environ,{'TOUR_API_KEY':'key','SUPABASE_URL':'url','SUPABASE_KEY':'key'},clear=True),contextlib.redirect_stdout(io.StringIO()):
                result=batch._run_batch(['--workers','4','--limit','1','--checkpoint',str(Path(directory)/'state.db')],report,context)
            self.assertEqual(result,0);self.assertEqual(active[1],4);self.assertEqual(report['endpoints_saved'],12);self.assertEqual(report['processed_count'],1)
            context['journal'].db.close()

    def test_catalog_v2_requests_100_and_keeps_food_and_lodging(self):
        session=Mock();session.get.return_value.json.return_value={'response':{'header':{'resultCode':'0000'},'body':{'items':{'item':[]},'totalCount':0}}}
        catalog.fetch_page(session,'encoded%2Bkey','areaBasedList2',{'pageNo':1})
        self.assertTrue(session.get.call_args.args[0].endswith('/KorService2/areaBasedList2'))
        self.assertEqual(session.get.call_args.kwargs['params']['numOfRows'],100)
        self.assertEqual(session.get.call_args.kwargs['params']['serviceKey'],'encoded+key')
        self.assertTrue({32,39}.issubset(catalog.TYPES))

if __name__=='__main__':unittest.main()
