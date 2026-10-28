"""Resumable, quota-accounted collection of every published Korean detail API.

Default storage uses the migration's transactional RPC. --storage-mode legacy is
an explicit compatibility mode for the existing schema; it preserves every raw
response under place_details.raw_details._tour_enrichment and existing images.
Completion and exception reports use Telegram when server-side credentials exist.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import sqlite3
import sys
import time
import threading
from functools import wraps
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

SERVICES = {
    "KorService2": ("detailCommon2", "detailIntro2", "detailInfo2", "detailImage2", "detailPetTour2"),
    "KorWithService2": ("detailWithTour2", "detailCommon2", "detailIntro2", "detailInfo2", "detailImage2"),
}
PRODUCTION_ENDPOINT_LIMIT = 100_000
DEVELOPMENT_ENDPOINT_LIMIT = 1_000

SUCCESS = {"0000", "00", "0"}


def kst_today():
    return datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%d')


class QuotaStop(RuntimeError):
    """Only a real provider quota response can stop collection."""
    def __init__(self, code, response_body):
        self.code = str(code)
        self.response_body = response_body
        super().__init__(f'TourAPI {self.code}\n{response_body}')


class EndpointBudgetStop(RuntimeError):
    """An operation's local daily budget or persisted provider block was reached."""


def operation(endpoint):
    return endpoint.split('.')[0]


def required_endpoints(place):
    result = {(service, endpoint) for service, endpoints in SERVICES.items() for endpoint in endpoints}
    if str(place['content_type_id']) == '39':
        result.update((service, 'detailImage2.menu') for service in SERVICES)
    return result


class ApiFailure(RuntimeError):
    def __init__(self, code):
        self.code = str(code)
        super().__init__(f"TourAPI result code {self.code}")


def items_from(payload):
    root = payload.get("response", payload)
    body = root.get("body") or {}
    if 'totalCount' not in body and str(root.get('header', {}).get('resultCode')) != '03':
        raise ValueError('TourAPI omitted pagination totalCount')
    items = body.get("items") or {}
    rows = items.get("item", []) if isinstance(items, dict) else []
    if rows is None or rows == '':
        rows = []
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Invalid TourAPI items shape")
    return rows, int(body.get("totalCount") or 0)


def journal_locked(method):
    @wraps(method)
    def protected(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return protected


class Journal:
    """Crash-safe page cache and per-operation daily budgets (KST)."""
    def __init__(self, path, key, global_limit=999999, service_limit=999999, force=False):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.execute("pragma journal_mode=WAL")
        self.db.executescript("""
          create table if not exists operation_calls (key_id text, day text, service text, endpoint text, count integer not null, primary key(key_id,day,service,endpoint));
          create table if not exists operation_blocks (key_id text, day text, service text, endpoint text, code text, primary key(key_id,day,service,endpoint));
          create table if not exists operation_policies (key_id text, service text, approved_limit integer not null, primary key(key_id,service));
          create table if not exists calls (key_id text, day text, service text, count integer not null, primary key(key_id,day,service));
          create table if not exists pages (place_id text, service text, endpoint text, page integer, payload text not null, primary key(place_id,service,endpoint,page));
          create table if not exists commits (place_id text, service text, endpoint text, digest text not null, primary key(place_id,service,endpoint));
          create table if not exists revisions (place_id text primary key, revision text);
          create table if not exists successful_places (key_id text, day text, place_id text, primary key(key_id,day,place_id));
        """)
        self.key_id = hashlib.sha256(key.encode()).hexdigest()[:20]
        self.endpoint_limit = PRODUCTION_ENDPOINT_LIMIT
        self.service_limits = {}
        self.ensure_day()

    @journal_locked
    def ensure_day(self):
        day = kst_today()
        self.db.executemany('insert or ignore into calls values(?,?,?,0)', [(self.key_id, day, service) for service in SERVICES])
        self.db.commit()
        return day

    @journal_locked
    def configure_policy(self, service, approved_limit):
        """Reopen old development quota blocks once when approval increases; keep counters."""
        previous = self.db.execute('select approved_limit from operation_policies where key_id=? and service=?', (self.key_id, service)).fetchone()
        old_limit = previous[0] if previous else DEVELOPMENT_ENDPOINT_LIMIT
        reopened = 0
        if approved_limit > old_limit:
            result = self.db.execute('delete from operation_blocks where key_id=? and day=? and service=?', (self.key_id, self.ensure_day(), service))
            reopened = result.rowcount
        self.db.execute('insert into operation_policies values(?,?,?) on conflict(key_id,service) do update set approved_limit=excluded.approved_limit', (self.key_id, service, approved_limit))
        self.db.commit()
        return {'service': service, 'previous_limit': old_limit, 'approved_limit': approved_limit, 'reopened_blocks': reopened}

    @journal_locked
    def revision(self, cid, revision, refresh=False):
        old = self.db.execute('select revision from revisions where place_id=?', (cid,)).fetchone()
        if refresh or (old and old[0] != revision):
            self.db.execute('delete from pages where place_id=?', (cid,))
            self.db.execute('delete from commits where place_id=?', (cid,))
        self.db.execute('insert or replace into revisions values(?,?)', (cid, revision))
        self.db.commit()

    @journal_locked
    def available(self, service, endpoint):
        day, endpoint = self.ensure_day(), operation(endpoint)
        blocked = self.db.execute('select 1 from operation_blocks where key_id=? and day=? and service=? and endpoint=?', (self.key_id, day, service, endpoint)).fetchone()
        row = self.db.execute('select count from operation_calls where key_id=? and day=? and service=? and endpoint=?', (self.key_id, day, service, endpoint)).fetchone()
        return not blocked and (row[0] if row else 0) < self.service_limits.get(service, self.endpoint_limit)

    @journal_locked
    def block(self, service, endpoint, code):
        self.db.execute('insert or replace into operation_blocks values(?,?,?,?,?)', (self.key_id, self.ensure_day(), service, operation(endpoint), code))
        self.db.commit()

    @journal_locked
    def endpoint_usage(self):
        day = self.ensure_day()
        rows = dict(((s, e), n) for s, e, n in self.db.execute('select service,endpoint,count from operation_calls where key_id=? and day=?', (self.key_id, day)))
        blocks = dict(((s, e), code) for s, e, code in self.db.execute('select service,endpoint,code from operation_blocks where key_id=? and day=?', (self.key_id, day)))
        return [{'service': service, 'endpoint': endpoint, 'calls': rows.get((service, endpoint), 0),
                 'limit': self.service_limits.get(service, self.endpoint_limit), 'image': endpoint == 'detailImage2',
                 'provider_block': blocks.get((service, endpoint))}
                for service, endpoints in SERVICES.items() for endpoint in endpoints]

    @journal_locked
    def reserve(self, service, endpoint=None):
        day = self.ensure_day()
        self.db.execute("begin immediate")
        try:
            if endpoint is not None:
                endpoint = operation(endpoint)
                day = kst_today()
                blocked = self.db.execute('select 1 from operation_blocks where key_id=? and day=? and service=? and endpoint=?', (self.key_id, day, service, endpoint)).fetchone()
                row = self.db.execute('select count from operation_calls where key_id=? and day=? and service=? and endpoint=?', (self.key_id, day, service, endpoint)).fetchone()
                if blocked or (row and row[0] >= self.service_limits.get(service, self.endpoint_limit)):
                    raise EndpointBudgetStop(f'{service}/{endpoint}: daily operation budget unavailable')
                self.db.execute('insert into operation_calls values(?,?,?,?,1) on conflict(key_id,day,service,endpoint) do update set count=count+1', (self.key_id, day, service, endpoint))
            self.db.execute("insert into calls values(?,?,?,1) on conflict(key_id,day,service) do update set count=count+1", (self.key_id, day, service))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    @journal_locked
    def cached(self, cid, service, endpoint, page):
        row = self.db.execute("select payload from pages where place_id=? and service=? and endpoint=? and page=?", (cid, service, endpoint, page)).fetchone()
        return json.loads(row[0]) if row else None

    @journal_locked
    def save(self, cid, service, endpoint, page, payload):
        self.db.execute("insert or replace into pages values(?,?,?,?,?)", (cid, service, endpoint, page, json.dumps(payload, ensure_ascii=False)))
        self.db.commit()

    @journal_locked
    def committed(self, cid, service, endpoint, digest):
        row = self.db.execute("select digest from commits where place_id=? and service=? and endpoint=?", (cid, service, endpoint)).fetchone()
        return bool(row and row[0] == digest)

    @journal_locked
    def mark(self, cid, service, endpoint, digest):
        self.db.execute("insert or replace into commits values(?,?,?,?)", (cid, service, endpoint, digest))
        self.db.commit()

    @journal_locked
    def day_calls(self):
        day = self.ensure_day()
        return dict(self.db.execute('select service,count from calls where key_id=? and day=?', (self.key_id, day)))

    @journal_locked
    def processed(self, cid):
        day = self.ensure_day()
        self.db.execute('insert or ignore into successful_places values(?,?,?)', (self.key_id, day, cid))
        self.db.commit()

    @journal_locked
    def day_processed(self):
        day = self.ensure_day()
        return self.db.execute('select count(*) from successful_places where key_id=? and day=?', (self.key_id, day)).fetchone()[0]


class Database:
    def __init__(self, url, key):
        self.url = url.rstrip("/") + "/rest/v1/"
        self.http = requests.Session()
        self.http.headers.update({"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"})

    def request(self, method, path, *, params=None, data=None, prefer=None):
        response = self.http.request(method, self.url + path, params=params, json=data,
                                     headers={"Prefer": prefer} if prefer else {}, timeout=40)
        if not response.ok:
            try:
                code = response.json().get("code", str(response.status_code))
            except ValueError:
                code = str(response.status_code)
            raise RuntimeError(f"Database {path.split('/')[0]} failed ({code}); apply enrichment migration if missing")
        return response.json() if response.content else None

    def rpc(self, name, data):
        return self.request("POST", "rpc/" + name, data=data)

    def count(self, table):
        response = self.http.head(self.url + table, params={'select': '*'}, headers={'Prefer': 'count=exact'}, timeout=40)
        response.raise_for_status()
        return int(response.headers['Content-Range'].split('/')[-1])

    def targets(self, cid=None, limit=300):
        """Include partially stored places so missing endpoints can resume."""
        selected = []
        while len(selected) < limit:
            params = {"select": "content_id,content_type_id,title,modified_time",
                      "order": "content_id",
                      "offset": len(selected), "limit": min(1000, limit - len(selected))}
            if cid:
                params["content_id"] = "eq." + cid
            rows = self.request("GET", "places", params=params)
            selected.extend(rows)
            if not rows or cid:
                break
        return selected

    def collected_ids(self):
        """Presence is independent of revision, endpoint count or empty responses."""
        return {str(row[0]) for row in self.completed()}

    def completed(self):
        found = set()
        offset = 0
        while True:
            rows = self.request('GET', 'place_tour_payloads', params={'select': 'place_id,source_service,endpoint,source_modified_time', 'order': 'place_id,source_service,endpoint', 'offset': offset, 'limit': 1000})
            if not rows:
                return found
            for row in rows:
                found.add((row['place_id'], row['source_service'], row['endpoint'], row['source_modified_time']))
            offset += len(rows)


class TourClient:
    def __init__(self, key, journal, db=None, delay=0.25, retries=3, global_limit=999999, service_limit=999999, force=False):
        self.key = unquote(key.strip())
        self.journal = journal
        self.db = db
        self.delay = delay
        self.retries = retries
        self.http = requests.Session()
        self.last_call = 0.0
        self.rate_lock = threading.Lock()
        self.worker_http = threading.local()
        self.deadline = None

    def page(self, service, endpoint, place, page):
        cid = str(place["content_id"])
        cached = self.journal.cached(cid, service, endpoint, page)
        if cached is not None:
            return cached
        params = {"serviceKey": self.key, "MobileOS": "ETC", "MobileApp": "FitTour", "_type": "json", "contentId": cid, "numOfRows": 100, "pageNo": page}
        if endpoint.startswith(("detailIntro", "detailInfo")):
            params["contentTypeId"] = str(place["content_type_id"])
        if endpoint.startswith("detailImage"):
            params.update(imageYN="N" if endpoint.endswith('.menu') else "Y")
        for attempt in range(self.retries):
            self.journal.reserve(service, endpoint)
            with self.rate_lock:
                time.sleep(max(0, self.delay - (time.monotonic() - self.last_call)))
                self.last_call = time.monotonic()
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise EndpointBudgetStop('Runtime deadline reached; checkpoint retained')
            try:
                http = self.http
                if threading.current_thread() is not threading.main_thread() and isinstance(http, requests.sessions.Session):
                    if not hasattr(self.worker_http, 'session'):
                        self.worker_http.session = requests.Session()
                    http = self.worker_http.session
                response = http.get(f"https://apis.data.go.kr/B551011/{service}/{endpoint.split('.')[0]}", params=params, timeout=(10, 30))
                if response.status_code == 429:
                    body = response.content.decode('utf-8-sig', errors='replace')
                    try:
                        header = json.loads(body).get('OpenAPI_ServiceResponse', {}).get('cmmMsgHeader', {})
                        code = header.get('returnReasonCode')
                    except (ValueError, AttributeError):
                        code = None
                    if str(code) == '23':
                        raise requests.ConnectionError('TourAPI per-second rate limit')
                    raise QuotaStop(f'HTTP 429 / {code}' if code else 'HTTP 429', body)
                if response.status_code >= 500:
                    raise requests.ConnectionError("TourAPI server error")
                if response.status_code in {401, 403}:
                    raise ApiFailure("20")
                try:
                    payload = json.loads(response.content.decode('utf-8-sig'))
                except ValueError:
                    # Gateway XML errors must never be mistaken for successful empty JSON.
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(response.content)
                    code = root.findtext(".//returnReasonCode") or root.findtext(".//resultCode") or "INVALID_RESPONSE"
                    if code == '22':
                        raise QuotaStop(code, response.content.decode('utf-8-sig', errors='replace'))
                    raise ApiFailure(code)
                code = str(payload.get("response", {}).get("header", {}).get("resultCode", payload.get('resultCode', payload.get('OpenAPI_ServiceResponse', {}).get('cmmMsgHeader', {}).get('returnReasonCode', "INVALID_RESPONSE"))))
                if code == "22":
                    raise QuotaStop(code, response.content.decode('utf-8-sig', errors='replace'))
                if code == "23":
                    raise requests.ConnectionError("TourAPI per-second rate limit")
                if code not in SUCCESS and code != "03":
                    raise ApiFailure(code)
                response.raise_for_status()
                items_from(payload)
                self.journal.save(cid, service, endpoint, page, payload)
                return payload
            except (requests.RequestException, ValueError) as exc:
                if attempt + 1 == self.retries:
                    raise RuntimeError(f"TourAPI {service}/{endpoint} failed after {self.retries} charged attempts ({type(exc).__name__})") from None
                time.sleep(min(30, 2 ** attempt + random.random()))
        raise AssertionError("unreachable")

    def collect(self, service, endpoint, place):
        pages, items = [], []
        for page_no in range(1, 10001):
            payload = self.page(service, endpoint, place, page_no)
            rows, total = items_from(payload)
            pages.append(payload)
            items.extend(rows)
            if len(items) >= total:
                return pages, items
            if not rows:
                raise RuntimeError("TourAPI stopped before advertised totalCount; checkpoint retained")
        raise RuntimeError("TourAPI pagination exceeded safety guard")


def image_rows(items):
    rows = []
    for item in items:
        candidates = [(item.get("originimgurl"), item.get("smallimageurl"), item.get("imgname"))]
        for key in ("firstimage", "subdetailimg", *(f"roomimg{i}" for i in range(1, 6))):
            candidates.append((item.get(key), item.get("firstimage2") if key == "firstimage" else None, item.get(key + "alt") or item.get("roomtitle") or item.get("title")))
        for origin, small, name in candidates:
            if origin and str(origin).startswith(("http://", "https://")):
                rows.append({"origin_image_url": origin, "small_image_url": small or None, "img_name": str(name or "")[:255] or None})
    return list({row["origin_image_url"]: row for row in rows}.values())


def legacy_store(db, place, service, endpoint, pages, items):
    """Explicit migration bridge. Each write is atomic; cross-table writes are resumable."""
    cid = str(place["content_id"])
    details = db.request("GET", "place_details", params={"select": "raw_details", "place_id": "eq." + cid})
    raw = (details[0].get("raw_details") if details else {}) or {}
    if not isinstance(raw, dict):
        raise ValueError("Existing raw_details is not an object; refusing to overwrite")
    if service == "KorService2" and endpoint == "detailIntro2" and items:
        raw.update(items[0])
    raw.setdefault("_tour_enrichment", {}).setdefault(service, {})[endpoint] = {"pages": pages, "items": items, "fetched_at": datetime.now(ZoneInfo("UTC")).isoformat()}
    db.request("POST", "place_details", params={"on_conflict": "place_id"}, data={"place_id": cid, "raw_details": raw}, prefer="resolution=merge-duplicates,return=minimal")
    if service == "KorService2" and endpoint == "detailCommon2" and items:
        item = items[0]
        columns = {"overview": "overview", "tel": "tel", "tour_first_image": "firstimage", "tour_first_image2": "firstimage2", "addr1": "addr1", "addr2": "addr2", "mapx": "mapx", "mapy": "mapy", "mlevel": "mlevel"}
        update = {column: item[key] for column, key in columns.items() if item.get(key) not in (None, "")}
        if update:
            db.request("PATCH", "places", params={"content_id": "eq." + cid}, data=update, prefer="return=minimal")
    new_images = image_rows(items)
    if new_images:
        existing = db.request("GET", "place_images", params={"select": "origin_image_url", "place_id": "eq." + cid})
        urls = {row["origin_image_url"] for row in existing}
        missing = [{"place_id": cid, **row} for row in new_images if row["origin_image_url"] not in urls]
        if missing:
            db.request("POST", "place_images", data=missing, prefer="return=minimal")


def enriched_count(completed, places=None):
    if places is not None:
        return sum(all((str(p["content_id"]), service, endpoint, str(p.get("modified_time") or "")) in completed
                       for service, endpoint in required_endpoints(p)) for p in places)
    required = {(service, endpoint) for service, endpoints in SERVICES.items() for endpoint in endpoints}
    revisions = {}
    for cid, service, endpoint, revision in completed:
        revisions.setdefault((cid, revision), set()).add((service, endpoint))
    return len({cid for (cid, _), endpoints in revisions.items() if required.issubset(endpoints)})


def telegram_report(report):
    total = report.get('total_enriched_count')
    population = report.get('master_total_count')
    population_label = f'{population:,}' if population is not None else '미확인'
    progress = f'{total / population * 100:.1f}' if total is not None and population else '미확인'
    return '\n'.join([
        '🔔 [FitTour TourAPI API별 수집 배치 보고]',
        f"• 실행 결과: {'실패' if report.get('exception_type') else '일부 실패' if report.get('failures') else '공사 쿼터 회복 대기' if report.get('quota_stopped') else 'API별 예산 대기' if report.get('local_budget_wait') else '성공'}",
        f"• 누적 보강 완료: {total if total is not None else '미확인'} / {population_label}곳 ({progress}%)",
        f"• 이번 실행 전체 완료: {report.get('processed_count', 0)}곳 / 부분 저장: {report.get('partial_saved_count', 0)}곳",
        f"• 이번 실행 이미지 외 정보 완료(전체 완료 포함): {report.get('non_image_complete_count', 0)}곳",
        f"• 신규 저장 시작(전체 완료와 다름): {report.get('actual_newly_inserted_count', '미확인')}곳",
        f"• 당일 전체 완료: {report.get('today_new_processed', '미확인')}곳",
        f"• 이번 실행 이미지 호출: {report.get('image_calls', 0)}회 / 기타 호출: {report.get('non_image_calls', 0)}회",
        '• API별 당일 호출 시도(일반·메뉴 이미지 합산, KST):',
        *[f"  {r['service']}/{r['endpoint']}: {r['calls']}/{r['limit']}회" + (' [공사 한도 응답]' if r.get('provider_block') else '') for r in report.get('endpoint_usage', [])],
        '• 호출 집계는 이 배치의 기록이며, 동일 키의 외부 사용량은 포함하지 않습니다.',
        f"• 중단/완료 상태: {report.get('status', '미확인')}",
        f"• 소요 시간: {report.get('elapsed_seconds', 0)}초",
        f"• 공사 API 응답 상태: {report.get('api_status', '미확인')}",
        f"• 이번 실행 저장: {report.get('endpoints_saved', 0)}개 엔드포인트 (신규 순증과 전체 보강 완료는 별도 집계)",
    ])


def send_telegram_report(report):
    token, chat_id = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        return False
    try:
        message = telegram_report(report)
        if 'api_response_body' in report:
            message += '\n' + report['api_response_body']
        # Keep the entire provider body, splitting below Telegram's UTF-16 length cap.
        for offset in range(0, len(message), 1800):
            response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                     json={'chat_id': chat_id, 'text': message[offset:offset + 1800]}, timeout=(5, 15))
            if response.status_code != 200 or response.json().get('ok') is not True:
                print('Telegram report delivery failed; batch result preserved.', file=sys.stderr)
                return False
        return True
    except Exception:
        # Request exceptions may contain the bot token in their URL; never print them.
        print('Telegram report delivery failed; batch result preserved.', file=sys.stderr)
        return False


def _run_batch(argv, report, context):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--content-id")
    parser.add_argument("--limit", type=int, default=50000, help="Maximum candidate places scanned; API budgets are independent")
    parser.add_argument("--endpoint-daily-limit", type=int, default=PRODUCTION_ENDPOINT_LIMIT)
    parser.add_argument("--with-endpoint-daily-limit", type=int, default=DEVELOPMENT_ENDPOINT_LIMIT)
    parser.add_argument("--workers", type=int, default=1, help="Bounded endpoint prefetch workers (1-8); workflows use 4")
    parser.add_argument("--max-seconds", type=float, default=2700)
    parser.add_argument("--storage-mode", choices=("rpc", "legacy"), default="rpc")
    parser.add_argument("--checkpoint", default="artifacts/tour-enrichment/checkpoint.sqlite3")
    parser.add_argument("--daily-limit", type=int, default=999999)
    parser.add_argument("--service-daily-limit", type=int, default=999999)
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--refresh", action="store_true", help="Refetch selected places, retaining all previous quota reservations")
    parser.add_argument('--force', '--ignore-quota', action='store_true', help='Legacy compatibility option; does not bypass per-API budgets')
    args = parser.parse_args(argv)
    if args.delay < 0 or args.limit < 1 or not 1 <= args.endpoint_daily_limit <= PRODUCTION_ENDPOINT_LIMIT or not 1 <= args.with_endpoint_daily_limit <= PRODUCTION_ENDPOINT_LIMIT or not 1 <= args.workers <= 8 or args.max_seconds <= 0:
        parser.error("delay >= 0, limit >= 1, daily limits 1..100000, workers 1..8, max-seconds > 0")
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    key = os.environ.get("TOUR_API_DECODING_KEY") or os.environ.get("TOUR_API_KEY")
    url = os.environ.get("SUPABASE_URL")
    db_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    if not all((key, url, db_key)):
        raise RuntimeError("Missing server-side TourAPI/Supabase environment configuration")
    db = Database(url, db_key)
    context['db'] = db
    report['master_total_count'] = db.count('places')
    for table in ('place_images', 'place_barrier_free'):
        try:
            context[table] = db.count(table)
        except Exception:
            context[table] = None
    if args.storage_mode == "rpc":
        # Fail before spending any TourAPI quota if migration is not installed.
        if db.rpc("tour_enrichment_preflight", {}) is not True:
            raise RuntimeError("Enrichment schema preflight failed")
    if args.storage_mode != 'rpc':
        parser.error('Resumable collection requires rpc storage and place_tour_payloads')
    completed = db.completed()
    existing_ids = {str(row[0]) for row in completed}
    catalog = list(db.targets(args.content_id, limit=500000))
    context['catalog'] = catalog if not args.content_id else None
    targets = [p for p in catalog if args.refresh or not all(
        (str(p['content_id']), service, endpoint, str(p.get('modified_time') or '')) in completed
        for service, endpoint in required_endpoints(p))][:args.limit]
    target_ids = [str(place['content_id']) for place in targets]
    if len(target_ids) != len(set(target_ids)) or len(target_ids) > args.limit:
        raise AssertionError('Duplicate IDs or oversized batch')
    context.update(existing_ids=existing_ids, target_ids=set(target_ids), saved_ids=set())
    report.update(target_ids=target_ids, actual_newly_inserted_count=None)
    print(f'신규 대상 {len(target_ids)}곳 추출 완료 (ID 범위: {target_ids[0] if target_ids else "-"} ~ {target_ids[-1] if target_ids else "-"})', flush=True)
    journal = Journal(args.checkpoint, key, args.daily_limit, args.service_daily_limit, force=args.force)
    journal.endpoint_limit = args.endpoint_daily_limit
    journal.service_limits['KorWithService2'] = args.with_endpoint_daily_limit
    report['quota_policy'] = [journal.configure_policy('KorService2', args.endpoint_daily_limit),
                              journal.configure_policy('KorWithService2', args.with_endpoint_daily_limit)]
    context['journal'] = journal
    context['initial_endpoint_usage'] = {(r['service'], r['endpoint']): r['calls'] for r in journal.endpoint_usage()}
    deadline = time.monotonic() + args.max_seconds
    client = TourClient(key, journal, db if args.storage_mode == "rpc" else None, delay=args.delay,
                        global_limit=args.daily_limit, service_limit=args.service_daily_limit, force=args.force)
    client.deadline = deadline
    executor = ThreadPoolExecutor(max_workers=args.workers) if args.workers > 1 else None
    context['executor'] = executor
    def prefetch(service, endpoint, place):
        try:
            return client.collect(service, endpoint, place)
        except QuotaStop as exc:
            journal.block(service, endpoint, exc.code)
            raise
    report.update(storage_mode=args.storage_mode, endpoints=[], quota_stopped=False, processed_count=0, limit=args.limit, api_status='정상')
    count, failures = 0, 0
    blocked_endpoints = set()
    report.update(partial_saved_count=0, no_progress_count=0, non_image_complete_count=0, time_budget_reached=False)
    report['quota_responses'] = []
    report['endpoints_saved'] = 0
    for place in targets:
        cid = str(place['content_id'])
        revision = str(place.get('modified_time') or '')
        required = required_endpoints(place)
        was_complete = all((cid, service, endpoint, revision) in completed for service, endpoint in required)
        if time.monotonic() >= deadline:
            report['time_budget_reached'] = True
            break
        if not args.refresh and was_complete:
            continue
        count += 1
        cid = str(place["content_id"])
        journal.revision(cid, str(place.get('modified_time') or ''), args.refresh)
        successful = set()
        saved_this_place = 0
        futures = {}
        if executor:
            for service, endpoint in sorted(required):
                if (service, operation(endpoint)) in blocked_endpoints or (not args.refresh and (cid, service, endpoint, revision) in completed):
                    continue
                futures[service, endpoint] = executor.submit(prefetch, service, endpoint, place)
        for service, endpoints in SERVICES.items():
            expanded = [variant for endpoint in endpoints for variant in ([endpoint, endpoint + '.menu'] if endpoint == 'detailImage2' and str(place['content_type_id']) == '39' else [endpoint])]
            for endpoint in expanded:
                entry = {"content_id": cid, "service": service, "endpoint": endpoint}
                revision = str(place.get('modified_time') or '')
                if not args.refresh and (cid, service, endpoint, revision) in completed:
                    successful.add((service, endpoint))
                    entry['status'] = 'already_committed'
                    report['endpoints'].append(entry)
                    continue
                if time.monotonic() >= deadline:
                    report['time_budget_reached'] = True
                    break
                if (service, operation(endpoint)) in blocked_endpoints:
                    continue
                try:
                    pages, items = futures[service, endpoint].result() if executor else client.collect(service, endpoint, place)
                    digest = hashlib.sha256(json.dumps(pages, sort_keys=True).encode()).hexdigest()
                    # Local marker is only set after all DB writes succeed.
                    if args.storage_mode == 'rpc' or not journal.committed(cid, service, endpoint, digest):
                        if args.storage_mode == "rpc":
                            db.rpc("upsert_tour_enrichment", {"p_place_id": cid, "p_service": service, "p_endpoint": endpoint, "p_pages": pages, "p_items": items, 'p_revision': revision})
                        else:
                            legacy_store(db, place, service, endpoint, pages, items)
                        journal.mark(cid, service, endpoint, digest)
                    successful.add((service, endpoint))
                    context['saved_ids'].add(cid)
                    report['endpoints_saved'] += 1
                    saved_this_place += 1
                    completed.add((cid, service, endpoint, revision))
                    entry.update(status="complete" if items else "empty", pages=len(pages), items=len(items))
                except ApiFailure as exc:
                    entry.update(status="api_error", code=exc.code)
                    failures += 1
                    report['api_status'] = exc.code
                except QuotaStop as exc:
                    blocked_endpoints.add((service, operation(endpoint)))
                    journal.block(service, endpoint, exc.code)
                    report['quota_responses'].append({'service': service, 'endpoint': operation(endpoint), 'code': exc.code, 'body': exc.response_body})
                    report.update(quota_stopped=True, api_status=' / '.join(
                        f"{row['service']}: {row['code']}" for row in report['quota_responses']))
                    report['api_response_body'] = '\n\n'.join(
                        f"{row['service']} ({row['code']})\n{row['body']}" for row in report['quota_responses'])
                    entry.update(status='provider_quota_wait', code=exc.code)
                    print(f'{service}: {exc}', flush=True)
                except EndpointBudgetStop:
                    blocked_endpoints.add((service, operation(endpoint)))
                    entry.update(status='local_budget_wait')
                except Exception as exc:
                    entry.update(status='error', error=type(exc).__name__)
                    failures += 1
                report['endpoints'].append(entry)
                print(json.dumps(entry, ensure_ascii=False), flush=True)
                if time.monotonic() >= deadline:
                    report['time_budget_reached'] = True
                    break
                if (service, operation(endpoint)) in blocked_endpoints:
                    continue
        if required.issubset(successful) and not was_complete:
            journal.processed(cid)
            report['processed_count'] += 1
        if saved_this_place:
            if not required.issubset(successful):
                report['partial_saved_count'] += 1
            if {(s, e) for s, e in required if operation(e) != 'detailImage2'}.issubset(successful):
                report['non_image_complete_count'] += 1
        else:
            report['no_progress_count'] += 1
    if executor:
        executor.shutdown(wait=True, cancel_futures=True)
        context['executor'] = None
    report.update(workers=args.workers, endpoint_daily_limit=args.endpoint_daily_limit)
    report.update(local_budget_wait=bool(blocked_endpoints), places_attempted=count, failures=failures,
                  blocked_endpoints=[{'service': s, 'endpoint': e} for s, e in sorted(blocked_endpoints)])
    # Provider quota exhaustion is an expected resumable outcome, not a batch failure.
    return 1 if failures or (args.content_id and count == 0) else 0


def main(argv=None):
    started = time.monotonic()
    report = {'started_at': datetime.now(ZoneInfo('UTC')).isoformat()}
    context = {}
    try:
        code = _run_batch(argv, report, context)
        report['status'] = ('일부 실패 (다음 배치에서 재시도)' if code else
                            '일일 한도 도달 (다음 배치 대기)' if report.get('quota_stopped') else
                            'API별 예산 소진 (다음 날 재개)' if report.get('local_budget_wait') else
                            '시간 예산 도달 (다음 배치 재개)' if report.get('time_budget_reached') else
                            '정상 완료 (다음 배치 대기)')
        return code
    except (Exception, KeyboardInterrupt) as exc:
        report.update(status=f'예외 종료 ({type(exc).__name__}, 재시도 필요)', exception_type=type(exc).__name__)
        raise
    finally:
        if context.get('executor'):
            context['executor'].shutdown(wait=True, cancel_futures=True)
        journal, db = context.get('journal'), context.get('db')
        if journal:
            try:
                calls = journal.day_calls()
                usage = journal.endpoint_usage()
                initial = context.get('initial_endpoint_usage', {})
                for row in usage:
                    row['run_calls'] = row['calls'] - initial.get((row['service'], row['endpoint']), 0)
                report.update(endpoint_usage=usage, image_calls=sum(r['run_calls'] for r in usage if r['image']),
                              non_image_calls=sum(r['run_calls'] for r in usage if not r['image']))
                report.update(day_calls={service: calls.get(service, 0) for service in SERVICES}, today_new_processed=journal.day_processed())
            except Exception:
                pass
        if db:
            try:
                report['master_total_count'] = db.count('places')
            except Exception:
                report['master_total_count'] = None
            try:
                after = db.completed()
                report['total_enriched_count'] = enriched_count(after, context.get('catalog'))
                if 'existing_ids' in context:
                    new_ids = ({str(row[0]) for row in after} - context['existing_ids']) & context['saved_ids']
                    report['actual_newly_inserted_count'] = len(new_ids)
            except Exception:
                pass
            for table, metric in (('place_images', 'images_count'), ('place_barrier_free', 'barrier_count')):
                try:
                    before = context.get(table)
                    report[metric] = max(0, db.count(table) - before) if before is not None else None
                except Exception:
                    report[metric] = None
        report.update(kst_date=kst_today(), elapsed_seconds=round(time.monotonic() - started, 1))
        report['finished_at'] = datetime.now(ZoneInfo('UTC')).isoformat()
        try:
            destination = Path('artifacts/tour-enrichment/last-run.json')
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError:
            print('Could not persist batch report.', file=sys.stderr)
        if 'status' in report:  # --help / invalid CLI arguments are not batch executions.
            console_encoding = getattr(sys.stdout, 'encoding', None) or 'utf-8'
            print(telegram_report(report).encode(console_encoding, errors='replace').decode(console_encoding), flush=True)
            summary = os.environ.get('GITHUB_STEP_SUMMARY')
            if summary:
                with open(summary, 'a', encoding='utf-8') as handle:
                    handle.write('```text\n' + telegram_report(report) + '\n```\n')
            send_telegram_report(report)
        if journal:
            journal.db.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Never print request URLs or exception strings containing API credentials.
        print(f"Enrichment stopped: {type(exc).__name__}. Check schema/environment; no secrets logged.", file=sys.stderr)
        raise SystemExit(1)
