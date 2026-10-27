"""TourAPI master sync. Install sql/places_universe_setup.sql first."""
import argparse
import logging
import math
import os
import time
from datetime import datetime, timedelta
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from supabase import create_client
from batch_enrich_cron import send_telegram_alert

LOG = logging.getLogger(__name__)
TYPES = (12, 14, 15, 25, 28, 32, 38, 39)
# Legacy TourAPI region codes -> current legal-dong province codes.
REGIONS = {"1": "11", "2": "28", "3": "27", "4": "30", "5": "29",
           "6": "26", "7": "31", "8": "36", "31": "41", "32": "51",
           "33": "43", "34": "44", "35": "52", "36": "46", "37": "47",
           "38": "48", "39": "50"}
BASE = "https://apis.data.go.kr/B551011/KorService2/"
KST = ZoneInfo("Asia/Seoul")
FIELDS = {"content_id": "contentid", "content_type_id": "contenttypeid",
          "title": "title", "addr1": "addr1", "addr2": "addr2",
          "zipcode": "zipcode", "tel": "tel", "first_image": "firstimage",
          "first_image2": "firstimage2", "area_code": "areacode",
          "sigungu_code": "sigungucode", "ldong_regn_cd": "lDongRegnCd",
          "mlevel": "mlevel", "modified_time": "modifiedtime"}


def map_item(item, active):
    row = {dest: str(item.get(src) or "").strip() or None
           for dest, src in FIELDS.items()}
    if not row["content_id"] or not row["modified_time"]:
        raise ValueError("TourAPI record missing identity or modification timestamp")
    datetime.strptime(row["modified_time"], "%Y%m%d%H%M%S")
    if active and (not row["title"] or int(row["content_type_id"] or 0) not in TYPES):
        raise ValueError("TourAPI active record missing title or valid category")
    try:
        x, y = float(item.get("mapx") or 0), float(item.get("mapy") or 0)
        valid = math.isfinite(x) and math.isfinite(y) and 124 <= x <= 132 and 33 <= y <= 38.6
    except (TypeError, ValueError):
        x = y = 0
        valid = False
    row.update(mapx=x if valid else None, mapy=y if valid else None, is_active=active)
    return row


def fetch_page(session, key, endpoint, params, sleep=time.sleep):
    for attempt in range(4):  # initial request + three retries
        try:
            response = session.get(BASE + endpoint, params={
                "serviceKey": unquote(key.strip()), "MobileOS": "ETC", "MobileApp": "LocalTripAI",
                "_type": "json", "numOfRows": 100, "arrange": "C", **params}, timeout=15)
            response.raise_for_status()
            payload = response.json()["response"]
            code = str(payload["header"]["resultCode"])
            if code not in ("0000", "00", "0"):
                raise ValueError("TourAPI result code " + code)
            body = payload["body"]
            total = int(body["totalCount"])
            wrapper = body.get("items") or {}
            items = wrapper.get("item") or []
            if isinstance(items, dict):
                items = [items]
            if not isinstance(items, list) or total < 0:
                raise ValueError("Malformed TourAPI page")
            return items, total
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
            # Do not log request exceptions: their URLs contain the service key.
            if attempt == 3:
                raise RuntimeError(f"TourAPI {endpoint} page {params['pageNo']} failed after 4 attempts") from None
            LOG.warning("TourAPI page retry %s/3", attempt + 1)
            sleep(2 ** attempt)


def run(db, api_key, area, mode, batch_size, session=None):
    started = datetime.now(KST)
    scope = "area:" + area
    state = db.rpc("places_universe_checkpoint", {"p_scope": scope}).execute().data
    since = state or (started - timedelta(days=1)).strftime("%Y%m%d")
    datetime.strptime(since, "%Y%m%d")
    totals = dict(inserted=0, updated=0, skipped=0, deactivated=0)
    endpoint = "areaBasedList2" if mode == "full" else "areaBasedSyncList2"
    http = session or requests.Session()
    # full establishes active universe; sync also processes tombstones.
    for flag in (("1",) if mode == "full" else ("1", "0")):
        for category in TYPES:
            page, consumed, expected = 1, 0, None
            seen = set()
            while expected is None or consumed < expected:
                params = {"pageNo": page, "contentTypeId": category}
                if area != "all":
                    params["lDongRegnCd"] = REGIONS[area]
                if mode == "sync":
                    params.update(showflag=flag, modifiedtime=since)
                items, total = fetch_page(http, api_key, endpoint, params)
                if expected is not None and total != expected:
                    raise RuntimeError("TourAPI population changed while paging; rerun safely")
                expected = total
                if not items and consumed < expected:
                    raise RuntimeError("TourAPI returned an incomplete page; checkpoint retained")
                rows = [map_item(item, flag == "1") for item in items]
                ids = [row["content_id"] for row in rows]
                if len(set(ids)) != len(ids) or seen.intersection(ids):
                    raise RuntimeError("TourAPI repeated records while paging; checkpoint retained")
                seen.update(ids)
                for offset in range(0, len(rows), batch_size):
                    counts = db.rpc("sync_places_universe_batch", {
                        "p_rows": rows[offset:offset + batch_size]}).execute().data
                    for name in totals:
                        totals[name] += counts[name]
                consumed += len(items)
                if consumed > expected:
                    raise RuntimeError("TourAPI count mismatch; checkpoint retained")
                LOG.info("category=%s active=%s page=%s fetched=%s/%s counts=%s",
                         category, flag, page, consumed, expected, totals)
                page += 1
    totals["total"] = db.table("places").select("content_id", count="exact", head=True).execute().count
    if totals["total"] is None:
        raise RuntimeError("DB total count unavailable")
    # Full does not consume tombstones and must not advance the incremental cursor.
    if mode == "sync":
        db.rpc("complete_places_universe_sync", {
            "p_scope": scope, "p_day": started.strftime("%Y%m%d")}).execute()
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", default="all")
    parser.add_argument("--mode", choices=("full", "sync"), default="full")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()
    if args.area != "all" and (not args.area.isdigit() or int(args.area) <= 0):
        parser.error("--area must be all or a positive region code")
    if not 1 <= args.batch_size <= 1000:
        parser.error("--batch-size must be between 1 and 1000")
    if args.area != "all":
        args.area = str(int(args.area))
        if args.area not in REGIONS:
            parser.error("--area must be a supported TourAPI region code")
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    started = time.monotonic()
    try:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
        api_key = os.getenv("TOUR_API_DECODING_KEY")
        if not all((url, key, api_key)):
            raise ValueError("Required Supabase / TourAPI configuration missing")
        counts = run(create_client(url, key), api_key, args.area, args.mode, args.batch_size)
        elapsed = round(time.monotonic() - started)
        report = ("[LocalTripAI] POI 마스터 동기화 완료\n"
                  f"신규 추가: {counts['inserted']}건 / 갱신: {counts['updated']}건 / "
                  f"비활성화: {counts['deactivated']}건 / 소요: {elapsed}초")
        LOG.info(report)
        send_telegram_alert(report)
        return 0
    except Exception as exc:
        report = f"[LocalTripAI] places sync failed ({type(exc).__name__}); rerun safely. Check setup SQL and API access."
        LOG.error(report)
        send_telegram_alert(report)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
