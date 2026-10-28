import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sync_places_universe import fetch_page, map_item, run, main, TYPES


def item(cid="1", **kwargs):
    return dict(contentid=cid, contenttypeid="12", title="Place",
                modifiedtime="20261014123000", mapx="126.9", mapy="37.5", **kwargs)


class UniverseTests(unittest.TestCase):
    def test_main_sends_completion_counts_and_elapsed(self):
        env = {'SUPABASE_URL': 'https://example.supabase.co',
               'SUPABASE_KEY': 'test-key', 'TOUR_API_DECODING_KEY': 'test-api-key'}
        counts = dict(inserted=12, updated=34, deactivated=5, skipped=0, total=100)
        with patch.dict('os.environ', env, clear=True), \
             patch('sys.argv', ['sync_places_universe.py', '--mode', 'sync']), \
             patch('sync_places_universe.load_dotenv'), \
             patch('sync_places_universe.create_client'), \
             patch('sync_places_universe.run', return_value=counts), \
             patch('sync_places_universe.time.monotonic', side_effect=[10, 18]), \
             patch('sync_places_universe.send_telegram_alert', return_value=False) as alert:
            self.assertEqual(main(), 0)
        alert.assert_called_once_with(
            '[LocalTripAI] POI 마스터 동기화 완료\n'
            '신규 추가: 12건 / 갱신: 34건 / 비활성화: 5건 / 소요: 8초')

    def test_main_failure_sends_failure_notification(self):
        with patch.dict('os.environ', {}, clear=True), \
             patch('sys.argv', ['sync_places_universe.py']), \
             patch('sync_places_universe.load_dotenv'), \
             patch('sync_places_universe.send_telegram_alert') as alert:
            self.assertEqual(main(), 1)
        alert.assert_called_once()
        self.assertIn('sync failed (ValueError)', alert.call_args.args[0])

    def test_invalid_coordinates_keep_master_record(self):
        for x in ("nan", "inf", "bad", "0", "200"):
            raw = item()
            raw["mapx"] = x
            mapped = map_item(raw, True)
            self.assertEqual(mapped["content_id"], "1")
            self.assertIsNone(mapped["mapx"])
            self.assertIsNone(mapped["mapy"])

    def test_festival_sanitization(self):
        raw = item()
        raw.update(contenttypeid="15", title="x" * 400, addr1="a" * 400,
                   addr2="b" * 400, tel="123\x00456")
        for value in (None, "", "bad", "nan", "inf", "200", [], {}):
            for field in ("mapx", "mapy"):
                candidate = dict(raw, **{field: value})
                mapped = map_item(candidate, True)
                self.assertIsNone(mapped["mapx"])
                self.assertIsNone(mapped["mapy"])
        for value in (None, "", "2026-10-28", "20260230", "20261301", "garbage", "????????"):
            raw.update(eventstartdate=value, eventenddate=value)
            mapped = map_item(raw, True)
            self.assertIsNone(mapped["eventstartdate"])
            self.assertIsNone(mapped["eventenddate"])
        raw.update(eventstartdate="20240229", eventenddate="20261028")
        mapped = map_item(raw, True)
        self.assertEqual(mapped["eventstartdate"], "20240229")
        self.assertEqual(mapped["eventenddate"], "20261028")
        self.assertEqual(len(mapped["title"]), 255)
        self.assertEqual(len(mapped["addr1"]), 120)
        self.assertEqual(mapped["tel"], "123456")
        self.assertEqual(mapped["mapx"], 126.9)

    def test_rejected_festival_continues_pages_and_later_categories(self):
        from postgrest.exceptions import APIError
        db = self.make_db()
        original = db.rpc.side_effect
        accepted = []
        def rpc(name, params):
            if name == "sync_places_universe_batch":
                rows = params["p_rows"]
                if any(r["content_id"] == "bad" for r in rows):
                    raise APIError({"code": "22007", "message": "invalid date", "details": None, "hint": None})
                accepted.extend(r["content_id"] for r in rows)
            return original(name, params)
        db.rpc.side_effect = rpc
        def fetch(http, key, endpoint, params):
            category, page = params["contentTypeId"], params["pageNo"]
            if category == 15 and page == 1:
                return [item("good1"), item("bad"), item("good2"), {"contentid": "malformed"}], 5
            if category == 15:
                return [item("next-page")], 5
            return [item(str(category))], 1
        with patch("sync_places_universe.fetch_page", side_effect=fetch), self.assertLogs(level="WARNING") as logs:
            counts = run(db, "key", "all", "full", 100)
        self.assertEqual(counts["skipped"], 2)
        self.assertEqual(counts["inserted"], 10)
        self.assertEqual(len(accepted), len(set(accepted)))
        self.assertTrue({"good1", "good2", "next-page", "32", "39"}.issubset(accepted))
        self.assertTrue(any("content_id=bad" in line for line in logs.output))

    def test_http_400_isolated_but_server_errors_propagate(self):
        import requests
        from sync_places_universe import sync_batch
        row = map_item(item(), True)
        for status in (400, 503):
            db = Mock()
            response = requests.Response()
            response.status_code = status
            db.rpc.return_value.execute.side_effect = requests.HTTPError(response=response)
            if status == 400:
                self.assertEqual(sync_batch(db, [row])["skipped"], 1)
            else:
                with self.assertRaises(requests.HTTPError):
                    sync_batch(db, [row])

    def test_tombstone_needs_no_title_or_category(self):
        mapped = map_item(dict(contentid="1", modifiedtime="20261014123000"), False)
        self.assertFalse(mapped["is_active"])

    def test_missing_timestamp_fails_instead_of_advancing(self):
        with self.assertRaises(ValueError):
            map_item(dict(contentid="1"), False)

    def test_retries_timeout_and_single_item(self):
        session = Mock()
        import requests
        response = Mock()
        response.json.return_value = {"response": {"header": {"resultCode": "0000"},
            "body": {"totalCount": "1", "items": {"item": item()}}}}
        session.get.side_effect = [requests.Timeout(), requests.Timeout(), requests.Timeout(), response]
        sleep = Mock()
        rows, total = fetch_page(session, "secret", "areaBasedSyncList2", {"pageNo": 1}, sleep)
        self.assertEqual((len(rows), total), (1, 1))
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2, 4])
        self.assertEqual(session.get.call_args.kwargs["timeout"], 15)

    def make_db(self):
        db = Mock()
        def rpc(name, params):
            data = "20261013" if name == "places_universe_checkpoint" else {
                "inserted": len(params.get("p_rows", [])), "updated": 0,
                "skipped": 0, "deactivated": 0}
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))
        db.rpc.side_effect = rpc
        db.table.return_value.select.return_value.execute.return_value.count = 1840
        return db

    def test_all_categories_flags_checkpoint_and_chunking(self):
        from unittest.mock import patch
        db = self.make_db()
        with patch("sync_places_universe.fetch_page", return_value=([item("1"), item("2")], 2)) as fetch:
            result = run(db, "key", "1", "sync", 1)
        self.assertEqual(fetch.call_count, len(TYPES) * 2)
        self.assertTrue(all(c.args[3]["lDongRegnCd"] == "11" and "areaCode" not in c.args[3]
                            for c in fetch.call_args_list))
        self.assertEqual({c.args[3]["showflag"] for c in fetch.call_args_list}, {"0", "1"})
        self.assertTrue(all(c.args[3]["modifiedtime"] == "20261013" for c in fetch.call_args_list))
        batches = [c for c in db.rpc.call_args_list if c.args[0] == "sync_places_universe_batch"]
        self.assertEqual(len(batches), 32)
        self.assertTrue(all(len(c.args[1]["p_rows"]) == 1 for c in batches))
        self.assertEqual(db.rpc.call_args_list[-1].args[0], "complete_places_universe_sync")
        self.assertEqual(result["total"], 1840)

    def test_failed_page_does_not_advance_checkpoint(self):
        from unittest.mock import patch
        db = self.make_db()
        with patch("sync_places_universe.fetch_page", return_value=([], 5)):
            with self.assertRaises(RuntimeError):
                run(db, "key", "all", "sync", 500)
        self.assertFalse(any(c.args[0] == "complete_places_universe_sync" for c in db.rpc.call_args_list))

    def test_full_does_not_advance_tombstone_checkpoint(self):
        from unittest.mock import patch
        db = self.make_db()
        with patch("sync_places_universe.fetch_page", return_value=([], 0)) as fetch:
            run(db, "key", "all", "full", 500)
        self.assertEqual(fetch.call_count, 8)
        self.assertTrue(all("areaCode" not in c.args[3] and "showflag" not in c.args[3] for c in fetch.call_args_list))
        self.assertFalse(any(c.args[0] == "complete_places_universe_sync" for c in db.rpc.call_args_list))

    def test_second_page_processed_and_repeated_page_rejected(self):
        from unittest.mock import patch
        db = self.make_db()
        with patch("sync_places_universe.fetch_page", side_effect=[([item("1")], 2), ([item("1")], 2)]):
            with self.assertRaises(RuntimeError):
                run(db, "key", "1", "sync", 500)
        batches = [c for c in db.rpc.call_args_list if c.args[0] == "sync_places_universe_batch"]
        self.assertEqual(len(batches), 1)
        self.assertFalse(any(c.args[0] == "complete_places_universe_sync" for c in db.rpc.call_args_list))

    def test_db_failure_does_not_advance_checkpoint(self):
        from unittest.mock import patch
        db = self.make_db()
        original = db.rpc.side_effect
        def fail(name, params):
            if name == "sync_places_universe_batch":
                raise RuntimeError("DB unavailable")
            return original(name, params)
        db.rpc.side_effect = fail
        with patch("sync_places_universe.fetch_page", return_value=([item()], 1)):
            with self.assertRaises(RuntimeError):
                run(db, "key", "all", "sync", 500)
        self.assertFalse(any(c.args[0] == "complete_places_universe_sync" for c in db.rpc.call_args_list))


if __name__ == "__main__":
    unittest.main()
