import contextlib
import io
import json
import pathlib
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch,Mock
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from mrt_collector import api_client as api, collector, enrichment as e, paged_collector as p, repository as r, category_search as cat, cli


class IntegratedFlowTests(unittest.TestCase):
 def con(self):
  c=sqlite3.connect(':memory:');api.init_db(c);collector.setup(c);e.setup(c);r.apply_schema(c);c.commit()
  self.addCleanup(c.close);return c

 def test_category_requires_real_city_evidence_and_preserves_checkpoint(self):
  c=self.con();doc={'request':{'city':'제주'},'parsed_content':{'categories':[{'name':'입장권','value':'ticket'}]}}
  self.assertEqual(cat.plan(c,'제주',doc),1)
  c.execute("update nationwide_jobs set next_page=3,status='PARTIAL'");c.commit()
  cat.plan(c,'제주',doc)
  self.assertEqual(c.execute('select next_page,status from nationwide_jobs').fetchone(),(3,'PARTIAL'))
  with self.assertRaises(ValueError):cat.plan(c,'서울',doc)

 def test_category_search_raw_product_dedup_and_next_page(self):
  c=self.con();cat.plan(c,'제주',{'request':{'city':'제주'},'parsed_content':{'categories':[{'value':'ticket'}]}})
  doc={'products':[{'productUrl':'https://experiences.myrealtrip.com/products/123','name':'티켓','salePrice':900}], 'pagination':{'hasNextPage':True}}
  client=Mock();client.call.return_value={'structuredContent':doc}
  with tempfile.TemporaryDirectory() as td,patch.object(api,'DATA',pathlib.Path(td)),patch.object(cat.time,'sleep'):
   cat.execute(c,client,1,0);cat.execute(c,client,1,0)
  self.assertEqual(c.execute('select count(*) from affiliate_products').fetchone()[0],1)
  self.assertEqual(c.execute('select next_page from nationwide_jobs').fetchone()[0],3)
  self.assertEqual(c.execute("select count(*) from product_observations where product_id is not null").fetchone()[0],2)
  self.assertEqual(client.call.call_args.args[1]['category'],'ticket')

 def test_enrichment_access_denied_stops_after_one_request(self):
  for code in (401,403):
   with self.subTest(code=code):
    c=self.con()
    for i in (1,2):
     c.execute("insert into affiliate_products values(?,1,?,'tna','T',?,'','','now','now','{}')",(i,str(i),'https://example.com/'+str(i)))
    c.commit();e.ensure_jobs(c,include_links=True)
    opts=types.SimpleNamespace(kind='link',product_type='all',retry_errors=True,confirm_links=True,max_calls=10,placement='default',delay=0)
    with tempfile.TemporaryDirectory() as td,patch.object(e,'OUT',pathlib.Path(td)),patch.object(e,'request',return_value={'http_status':code,'body':{}}) as request:
     e.run(c,opts)
     self.assertEqual(request.call_count,1)
    self.assertEqual(c.execute("select status from enrichment_runs where product_id=2 and kind='link'").fetchone()[0],'PENDING')

 def test_429_retains_work_and_bounded_retry(self):
  denied={'http_status':429,'headers':{'Retry-After':'3'},'body':{}}
  success={'http_status':200,'body':{'data':{'items':[]}}}
  with patch.object(p,'request',side_effect=[denied,success]) as call,patch.object(p.time,'sleep') as sleep:
   self.assertEqual(p.call_bounded('/verified',{},1,1),success)
   self.assertEqual(call.call_count,2);sleep.assert_called_once_with(3)
  c=self.con()
  for i in (1,2):c.execute("insert into nationwide_jobs(job_id,phase,region_json,keyword,next_page,status,updated_at) values(?,'tna',?,'q',2,'PENDING','now')",(str(i),json.dumps({'sido':'서울특별시','query':'서울'})))
  c.commit()
  with patch.object(collector,'load_env'),patch.object(collector,'request',return_value=denied) as call:
   collector.execute(c,'tna',10,3,0);self.assertEqual(call.call_count,1)
  self.assertEqual(c.execute("select next_page,status from nationwide_jobs where job_id='1'").fetchone(),(2,'RETRY'))

 def test_search_access_denied_stops_all_jobs(self):
  c=self.con()
  for i in (1,2):c.execute("insert into nationwide_jobs(job_id,phase,region_json,keyword,next_page,status,updated_at) values(?,'tna',?,'q',1,'PENDING','now')",(str(i),json.dumps({'sido':'서울특별시','query':'서울'})))
  c.commit()
  with patch.object(collector,'load_env'),patch.object(collector,'request',return_value={'http_status':403,'body':{}}) as call:
   collector.execute(c,'tna',10,3,0);self.assertEqual(call.call_count,1)

 def test_new_observations_survive_failed_link_reuse(self):
  c=self.con()
  c.execute("insert into affiliate_products values(1,1,'1','tna','T','https://example.com/1','','','now','now','{}')")
  c.execute("insert into affiliate_links values(1,1,1,'https://example.com/1','https://myrealt.rip/keep','old','now')");c.commit()
  e.ensure_jobs(c,include_links=True)
  with tempfile.TemporaryDirectory() as td,patch.object(e,'OUT',pathlib.Path(td)):
   e.mark(c,1,'link','ERROR',response={'http_status':403},error='HTTP 403',source='mylink')
   e.act(c,'link',e.rows(c)[0],None,types.SimpleNamespace(placement='default'))
  self.assertEqual(c.execute('select has_link,last_attempt_status,last_attempt_error from collector_link_state').fetchone(),(1,'ERROR','HTTP 403'))

 def test_cli_export_uses_isolated_database(self):
  with tempfile.TemporaryDirectory() as td:
   db=pathlib.Path(td)/'db'
   with contextlib.closing(sqlite3.connect(db)) as c:api.init_db(c);e.setup(c);collector.setup(c);r.apply_schema(c);c.commit()
   with contextlib.redirect_stdout(io.StringIO()):
    cli.main(['status','--db',str(db)])
    cli.main(['export','--db',str(db),'--out',td+'/out'])
    cli.main(['db-audit','--db',str(db),'--out',td+'/out'])
   self.assertTrue((pathlib.Path(td)/'out/manifest.json').exists())

 def test_full_sqlite_schema_creates_a_standalone_database(self):
  c=sqlite3.connect(':memory:');self.addCleanup(c.close)
  c.executescript((r.ROOT/'db/sqlite_schema.sql').read_text(encoding='utf-8'))
  self.assertTrue(r.ready(c))
  self.assertEqual(c.execute('select code from affiliate_companies where id=1').fetchone()[0],'MYREALTRIP')
  self.assertEqual(list(c.execute('pragma foreign_key_check')),[])

 def test_operating_package_has_no_legacy_or_archive_import_dependency(self):
  import ast
  forbidden={'mrt_analyzer','discover_api','enrich_existing','collector_v4','collector_stays_v44','nationwide_collect','collector_audit','final_capability_probe'}
  for file in (r.ROOT/'src/mrt_collector').glob('*.py'):
   tree=ast.parse(file.read_text(encoding='utf-8'))
   for node in ast.walk(tree):
    if isinstance(node,ast.ImportFrom) and node.level==0:self.assertNotIn(node.module,forbidden,str(file))
    if isinstance(node,ast.Import):
     for alias in node.names:self.assertNotIn(alias.name,forbidden,str(file))


if __name__=='__main__':unittest.main()
