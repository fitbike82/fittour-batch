"""Quota-safe daily enrichment. See BATCH_SETUP.md before enabling cron."""
import argparse
import html
import io
import logging
import os
import re
import time
import traceback
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from PIL import Image, ImageOps
from supabase import create_client

LOG = logging.getLogger(__name__)
MAX_PLACES = 950
MAX_CALLS_PER_ENDPOINT = 950
ENDPOINTS = ("detailCommon2", "detailIntro2", "detailInfo2")


class BudgetExhausted(RuntimeError):
    pass


def send_telegram_alert(message):
    """Best-effort notification; missing secrets or Telegram failures never stop ETL."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        LOG.info("Telegram secrets missing; console notification:\n%s", message)
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message}, timeout=15)
        response.raise_for_status()
        if response.json().get("ok") is not True:
            LOG.warning("Telegram rejected notification")
            return False
        return True
    except Exception as exc:
        # Request exceptions include the token-bearing URL. Never log the body.
        LOG.warning("Telegram notification failed: %s", type(exc).__name__)
        return False


def error_alert(message):
    send_telegram_alert(
        "🚨 [LocalTripAI] 일일 배치 에러 발생\n"
        f"- 에러 내용: {message}\n"
        "- 확인 요망: GitHub Actions 실행 로그")


def clean(value):
    return html.unescape(re.sub(r"<[^>]*>", " ", str(value or ""))).strip()


def load_batch_config():
    url = os.getenv("SUPABASE_URL", "").strip()
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    key = service_key or os.getenv("SUPABASE_KEY", "").strip()
    api_key = os.getenv("TOUR_API_DECODING_KEY", "").strip()
    missing = [name for name, value in (
        ("SUPABASE_URL", url),
        ("SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY)", key),
        ("TOUR_API_DECODING_KEY", api_key)) if not value]
    if missing:
        raise ValueError("Missing GitHub Secrets: " + ", ".join(missing))
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("SUPABASE_URL must be the project HTTPS base URL, not a dashboard/API path")
    if not key.startswith(("eyJ", "sb_secret_", "sb_publishable_")):
        raise ValueError("Invalid Supabase key format: supply an API key, not a URL or placeholder")
    if key.startswith("sb_publishable_"):
        LOG.warning("Publishable key selected; batch RPC requires service_role permission")
    LOG.info("Preflight configuration OK; key source=%s", "SUPABASE_SERVICE_ROLE_KEY" if service_key else "SUPABASE_KEY")
    return url, key, api_key


class Batch:
    def __init__(self, db, api_key, url, session=None):
        self.db, self.api_key = db, api_key
        self.url = url.rstrip("/")
        self.http = session or requests.Session()
        self.day = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
        self.processed = 0
        self.errors = []
        self.quota_stopped = False
        self.api_quota = dict.fromkeys(ENDPOINTS, 0)

    def preflight(self):
        """Read-only checks; no quota reservation or TourAPI call."""
        checks = {
            "places": "content_id,content_type_id,first_image,overview,tel,is_active,is_image_stored,enrichment_completed,enrichment_attempted_at",
            "place_details": "place_id,business_hours,day_off,parking_available,chk_credit_card,first_menu,treat_menu,check_in_time,check_out_time,raw_details",
        }
        for table, columns in checks.items():
            LOG.info("Preflight checking public.%s columns: %s", table, columns)
            try:
                self.db.table(table).select(columns).limit(1).execute()
            except Exception as exc:
                raise RuntimeError(
                    f"Preflight public.{table}: required table/columns or SELECT access missing. "
                    "Verify V2.1 base DDL and sql/enrich_batch_setup.sql; "
                    f"original error: {exc}") from exc
        LOG.info("Preflight checking checkpoint table and budget RPC (read-only)")
        try:
            result = self.db.rpc("preflight_enrich_batch", {}).execute().data
            if result is not True:
                raise ValueError("preflight_enrich_batch must return true")
        except Exception as exc:
            raise RuntimeError(
                "Preflight checkpoint/RPC failed: reapply sql/enrich_batch_setup.sql; "
                "requires localtrip_batch.endpoint_checkpoints, reserve_enrich_endpoint_budget and "
                f"preflight_enrich_batch with service_role EXECUTE access. Original error: {exc}") from exc
        LOG.info("Preflight checking Storage bucket places")
        try:
            bucket = self.db.storage.get_bucket("places")
            public = bucket.get("public") if isinstance(bucket, dict) else bucket.public
            if public is not True:
                raise ValueError("Storage bucket 'places' exists but is not public")
        except Exception as exc:
            raise RuntimeError(
                "Preflight Storage failed: create public bucket 'places' and provide "
                f"a backend key with Storage access. Original error: {exc}") from exc
        LOG.info("Preflight passed; upload/readback verified separately for each image")

    def completion_counts(self):
        # PostgREST accepts 206 HEAD responses and reads the exact count from
        # Content-Range; the returned page length is not the total count.
        total = (self.db.table("places").select("content_id", count="exact", head=True)
                 .eq("enrichment_completed", True).execute().count)
        remaining = (self.db.table("places").select("content_id", count="exact", head=True)
                     .eq("is_active", True).eq("enrichment_completed", False)
                     .or_("is_image_stored.eq.false,overview.is.null").execute().count)
        if total is None or remaining is None:
            raise ValueError("Batch counts unavailable")
        return total, remaining

    def reserve(self, endpoint):
        # Atomic persistent reservations happen BEFORE network calls. A killed run
        # conservatively spends its reservation rather than resetting the budget.
        if endpoint not in self.api_quota:
            raise ValueError(f"Unsupported TourAPI endpoint: {endpoint}")
        result = self.db.rpc("reserve_enrich_endpoint_budget", {
            "p_day": self.day, "p_endpoint": endpoint,
        }).execute().data
        if result is not True:
            raise BudgetExhausted()
        self.api_quota[endpoint] += 1

    def detail(self, endpoint, place):
        self.reserve(endpoint)
        params = {"serviceKey": self.api_key, "MobileOS": "ETC",
                  "MobileApp": "LocalTripAI", "_type": "json",
                  "contentId": str(place["content_id"])}
        if endpoint == "detailIntro2":
            params["contentTypeId"] = str(place["content_type_id"])
        response = self.http.get(
            f"https://apis.data.go.kr/B551011/KorService2/{endpoint}",
            params=params, timeout=5)
        if response.status_code == 429:
            raise BudgetExhausted("TourAPI HTTP 429 quota exceeded")
        response.raise_for_status()
        payload = response.json()["response"]
        code = str(payload["header"]["resultCode"])
        if code in {"22", "30", "31", "32"}:
            if code == "22":
                raise BudgetExhausted("TourAPI daily quota exceeded")
            raise RuntimeError(f"TourAPI error code {code}")
        if code not in {"0000", "00", "0"}:
            raise ValueError("TourAPI unsuccessful response")
        items = payload.get("body", {}).get("items") or {}
        item = items.get("item", []) if isinstance(items, dict) else []
        if isinstance(item, list):
            item = item[0] if item else {}
        if not item:
            raise ValueError("TourAPI detail missing")
        return item

    def image(self, place, common):
        cid = str(place["content_id"])
        if not re.fullmatch(r"[0-9]+", cid):
            raise ValueError("Invalid content_id")
        bucket = self.db.storage.from_("places")
        path = f"{cid}.webp"
        prefix = f"{self.url}/storage/v1/object/public/places/"
        source = str(place.get("first_image") or common.get("firstimage") or "").strip()
        if not source:
            # No source is not a successful upload. Keep flag false; successful
            # detail completion excludes it until the source changes.
            return None
        if not source.startswith(prefix):
            response = self.http.get(source, timeout=5)
            response.raise_for_status()
            with Image.open(io.BytesIO(response.content)) as original:
                image = ImageOps.exif_transpose(original).convert("RGB")
                if image.width > 800:
                    image = image.resize((800, max(1, round(image.height * 800 / image.width))),
                                         Image.Resampling.LANCZOS)
                output = io.BytesIO()
                image.save(output, "WEBP", quality=80)
            bucket.upload(path, output.getvalue(), file_options={
                "content-type": "image/webp", "upsert": "true"})
        else:
            path = source[len(prefix):].split("?")[0]
        # Authenticated readback checks existence, decoding and dimensions.
        with Image.open(io.BytesIO(bucket.download(path))) as stored:
            if stored.format != "WEBP" or stored.width > 800:
                raise ValueError("Stored WebP verification failed")
            stored.load()
        url = bucket.get_public_url(path)
        public = self.http.get(url, timeout=5)
        public.raise_for_status()
        if not public.content:
            raise ValueError("Public Storage object is empty")
        return url

    def enrich(self, place):
        common = self.detail("detailCommon2", place)
        intro = self.detail("detailIntro2", place)
        cid, kind = str(place["content_id"]), str(place["content_type_id"])
        fields = {
            "12": {"business_hours": "usetime", "day_off": "restdate",
                   "parking_available": "parking", "chk_credit_card": "chkcreditcard"},
            "39": {"business_hours": "opentimefood", "day_off": "restdatefood",
                   "parking_available": "parkingfood", "chk_credit_card": "chkcreditcardfood",
                   "first_menu": "firstmenu", "treat_menu": "treatmenu"},
            "32": {"check_in_time": "checkintime", "check_out_time": "checkouttime",
                   "parking_available": "parkinglodging", "chk_credit_card": "chkcreditcard"},
        }
        details = {key: clean(intro.get(value)) or None for key, value in fields.get(kind, {}).items()}
        details.update(place_id=cid, raw_details=intro)
        self.db.table("place_details").upsert(details, on_conflict="place_id").execute()
        image_url = self.image(place, common)
        patch = {"overview": clean(common.get("overview")) or None,
                 "is_image_stored": bool(image_url), "enrichment_completed": True}
        if image_url:
            patch["first_image"] = image_url
        if common.get("tel"):
            patch["tel"] = clean(common["tel"])
        result = self.db.table("places").update(patch).eq("content_id", cid).execute()
        if not result.data:
            raise ValueError("Place update returned no rows")

    def run(self, limit):
        if not 1 <= limit <= MAX_PLACES:
            raise ValueError("limit must be 1..950")
        self.preflight()
        targets = (self.db.table("places").select(
            "content_id,content_type_id,first_image,overview,is_image_stored")
            .eq("is_active", True).eq("enrichment_completed", False)
            .or_("is_image_stored.eq.false,overview.is.null")
            .order("enrichment_attempted_at", nullsfirst=True).order("content_id")
            .limit(limit).execute().data)
        for place in targets:
            try:
                # Persist attempt order to prevent failing sources starving the queue.
                self.db.table("places").update({"enrichment_attempted_at":
                    datetime.now(ZoneInfo("UTC")).isoformat()}).eq(
                        "content_id", place["content_id"]).execute()
                self.enrich(place)
                self.processed += 1
                LOG.info("Completed content_id=%s", place["content_id"])
            except BudgetExhausted:
                self.quota_stopped = True
                LOG.info("Endpoint daily quota reached; resume on next scheduled day; this run=%s", self.api_quota)
                break
            except Exception as exc:
                self.errors.append(f"content_id={place['content_id']} ({type(exc).__name__})")
                LOG.error("Failed content_id=%s: %s: %s", place["content_id"], type(exc).__name__, exc)
                traceback.print_exc()
        # Individual failures remain retryable; quota stops are normal completion.
        return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=900, choices=range(1, MAX_PLACES + 1))
    args = parser.parse_args()
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    started = time.monotonic()
    send_telegram_alert(
        "🔔 [LocalTripAI] 일일 배치 수집 시작\n"
        f"- 처리 일시: {datetime.now(ZoneInfo('Asia/Seoul')):%Y-%m-%d %H:%M:%S} (KST)\n"
        f"- 처리 한도: {args.limit}건")
    try:
        LOG.info("Preflight validating environment configuration")
        url, key, api_key = load_batch_config()
        LOG.info("Initializing Supabase client")
        batch = Batch(create_client(url, key), api_key, url)
        batch.run(args.limit)
        try:
            total, remaining = batch.completion_counts()
        except Exception as exc:
            # Reporting must not turn a completed batch or safe stop into failure.
            LOG.warning("Completion counts unavailable: %s", type(exc).__name__)
            total = remaining = "집계 불가"
        elapsed = round(time.monotonic() - started)
        minutes, seconds = divmod(elapsed, 60)
        status = "정상 완료 (WebP 변환 및 상세 스펙 적재 성공)"
        if batch.quota_stopped:
            status = "일일 안전 한도 도달 (다음 예약 실행에서 이어받기)"
        elif batch.processed == 0:
            status = "정상 완료 (처리 대상 없음)"
        elif batch.processed == args.limit:
            status = f"당일 목표({args.limit}건) 완수"
        if batch.errors and not batch.quota_stopped:
            status = f"부분 완료 (성공: {batch.processed}건 / 실패: {len(batch.errors)}건)"
        send_telegram_alert(
            "🔔 [LocalTripAI] 일일 배치 수집 완료\n"
            f"- 처리 일시: {datetime.now(ZoneInfo('Asia/Seoul')):%Y-%m-%d %H:%M:%S} (KST)\n"
            f"- 당일 처리: {batch.processed}건\n"
            f"- 누적 완료: {total}건\n"
            f"- 잔여 대상: {remaining}건\n"
            f"- 소요 시간: {minutes}분 {seconds}초\n"
            f"- 상태: {status}")
        if batch.errors:
            error_alert(f"{len(batch.errors)}건 처리 실패: " + "; ".join(batch.errors[:5]))
        return 0
    except Exception as exc:
        LOG.error("Batch stopped: %s: %s", type(exc).__name__, exc)
        traceback.print_exc()
        error_alert(f"{type(exc).__name__} (환경변수, DB 스키마 및 Storage 확인 필요)")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
