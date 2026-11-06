import importlib
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import closing

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from mrt_collector import repository as r


class RepositoryTests(unittest.TestCase):
 def database(self,path):
  from mrt_collector.api_client import init_db
  from mrt_collector.enrichment import setup as enrich_setup
  from mrt_collector.collector import setup as search_setup
  con=sqlite3.connect(path);init_db(con);enrich_setup(con);search_setup(con)
  con.execute("INSERT INTO affiliate_products VALUES(1,1,'123','tna','TEST','https://example.com/123','','','2026-11-01T00:00:00Z','2026-11-01T00:00:00Z',?)",(json.dumps({'salePrice':12000,'unknown':{'newField':[1,2]}}),))
  con.execute("INSERT INTO affiliate_links VALUES(1,1,1,'https://example.com/123','https://myrealt.rip/keep','old','2026-11-01T00:00:00Z')")
  con.commit();return con

 def test_migration_preserves_every_existing_row_and_is_idempotent(self):
  with tempfile.TemporaryDirectory() as td:
   path=pathlib.Path(td)/'data/inventory.sqlite3';path.parent.mkdir()
   con=self.database(path);con.execute('PRAGMA journal_mode=WAL');con.commit()
   before=r.snapshot(con);con.close()
   first=r.migrate(path);self.assertTrue(first['preserved'])
   with closing(sqlite3.connect(first['backup'])) as backup:self.assertEqual(r.snapshot(backup),before)
   with closing(sqlite3.connect(path)) as con:count=con.execute('select count(*) from product_observations').fetchone()[0]
   second=r.migrate(path);self.assertTrue(second['preserved'])
   with closing(sqlite3.connect(path)) as con:
    self.assertEqual(r.snapshot(con,list(before)),before)
    self.assertEqual(con.execute('select count(*) from product_observations').fetchone()[0],count)

 def test_prices_keep_value_type_conditions_and_history(self):
  with tempfile.TemporaryDirectory() as td:
   con=self.database(pathlib.Path(td)/'db');r.apply_schema(con)
   for date,price in [('2026-12-01','100.50'),('2026-12-02',90)]:
    obj={'rooms':[{'name':'room','salePrice':price,'currency':'KRW','soldOut':False}], 'newField':{'array':[1,None,'x']}}
    r.record(con,1,'detail','getStayDetail',obj,{'checkIn':date},date)
   values=con.execute('select value_json,request_json from product_prices order by id').fetchall()
   self.assertEqual([json.loads(v[0]) for v in values],['100.50',90])
   self.assertNotEqual(values[0][1],values[1][1])
   self.assertEqual(json.loads(con.execute('select payload_json from product_observations order by id limit 1').fetchone()[0])['newField']['array'],[1,None,'x'])
   con.close()

 def test_no_location_or_currency_fabrication(self):
  with tempfile.TemporaryDirectory() as td:
   con=self.database(pathlib.Path(td)/'db');r.apply_schema(con)
   r.record(con,1,'search','api',{'salePrice':0,'city':'서울'})
   self.assertEqual(con.execute('select count(*) from product_locations').fetchone()[0],0)
   self.assertIsNone(con.execute('select currency from product_prices').fetchone()[0])
   con.close()

 def test_export_roundtrip_all_fields(self):
  with tempfile.TemporaryDirectory() as td:
   con=self.database(pathlib.Path(td)/'db');r.apply_schema(con)
   r.record(con,1,'detail','mcp',{'html':'<b>한글</b>','options':[{'quantity':None}]})
   out=pathlib.Path(td)/'export';manifest=r.export_tables(con,out)
   for name in r.tables(con):
    rows=[json.loads(line) for line in (out/(name+'.jsonl')).read_text(encoding='utf-8').splitlines()]
    self.assertEqual(len(rows),manifest[name]['rows'])
    self.assertEqual(list(con.execute('select * from '+r.quote(name))),[tuple(row[k] for k in manifest[name]['columns']) for row in rows])
   con.close()

 def test_link_presence_separate_from_last_failure(self):
  with tempfile.TemporaryDirectory() as td:
   con=self.database(pathlib.Path(td)/'db');r.apply_schema(con)
   con.execute("insert into enrichment_runs(product_id,kind,status,updated_at,error) values(1,'link','ERROR','now','HTTP 403')")
   self.assertEqual(con.execute('select has_link,last_attempt_status from collector_link_state where product_id=1').fetchone(),(1,'ERROR'))
   con.close()

 def test_transaction_rollback_on_backfill_failure(self):
  with tempfile.TemporaryDirectory() as td:
   path=pathlib.Path(td)/'db';con=self.database(path);before=r.snapshot(con);con.close()
   with patch.object(r,'backfill',side_effect=RuntimeError('test failure')):
    with self.assertRaises(RuntimeError):r.migrate(path)
   with closing(sqlite3.connect(path)) as con:self.assertEqual(r.snapshot(con),before)


if __name__=='__main__':unittest.main()
