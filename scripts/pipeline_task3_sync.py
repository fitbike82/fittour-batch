"""
TASK-003: TourAPI 4.4 벌크 수집 및 일일 증분 동기화 파이프라인
- Supabase 마스터 DDL(V2.1) 기반 멱등 적재 (Upsert)
- 원칙 준수: 0원 인프라 유지, 타입 안전성, 멱등성
"""

import os
import sys
import time
import argparse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from supabase import create_client, Client

# Windows 콘솔 UTF-8 입출력 인코딩 보장
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 1. 환경변수 로드
load_dotenv()

TOUR_API_KEY = os.getenv("TOUR_API_KEY") or os.getenv("TOUR_API_DECODING_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not all([TOUR_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    print("[오류] 필수 환경변수가 누락되었습니다.", file=sys.stderr)
    print(f"  - TOUR_API_KEY: {'OK' if TOUR_API_KEY else 'MISSING'}", file=sys.stderr)
    print(f"  - SUPABASE_URL: {'OK' if SUPABASE_URL else 'MISSING'}", file=sys.stderr)
    print(f"  - SUPABASE_KEY: {'OK' if SUPABASE_KEY else 'MISSING'}", file=sys.stderr)
    sys.exit(1)

# Supabase 클라이언트 초기화
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# TourAPI 4.4 엔드포인트 상수 정의
TOUR_API_BASE_URL = "https://apis.data.go.kr/B551011/KorService2"
AREA_BASED_LIST_URL = f"{TOUR_API_BASE_URL}/areaBasedList2"
AREA_BASED_SYNC_URL = f"{TOUR_API_BASE_URL}/areaBasedSyncList2"

# 관광 타입 매핑 (로그 가독성용)
CONTENT_TYPE_NAMES = {
    "12": "관광지",
    "32": "숙박",
    "39": "음식점",
}


def fetch_api_with_retry(
    url: str,
    params: Dict[str, Any],
    max_retries: int = 3,
    base_timeout: float = 10.0,
    backoff_factor: float = 2.0,
) -> Optional[dict]:
    """
    타임아웃(10초) 및 실패 시 지수 백오프(최대 3회 재시도)를 적용한 HTTP GET 요청 함수.
    """
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, params=params, timeout=base_timeout)
            
            if response.status_code == 200:
                try:
                    data = response.json()
                except Exception as json_err:
                    print(f"  [경고] JSON 파싱 실패 (시도 {attempt}/{max_retries}): {json_err}")
                    if attempt < max_retries:
                        time.sleep(backoff_factor ** (attempt - 1))
                        continue
                    return None

                # TourAPI Header 검증
                header = data.get("response", {}).get("header", {})
                result_code = header.get("resultCode")
                if result_code == "0000":
                    return data
                else:
                    result_msg = header.get("resultMsg", "Unknown Error")
                    print(f"  [API 오류 응답] 시도 {attempt}/{max_retries}: Code={result_code}, Msg={result_msg}")
            else:
                print(f"  [HTTP 상태 오류] 시도 {attempt}/{max_retries}: HTTP {response.status_code}")

        except requests.exceptions.RequestException as req_err:
            print(f"  [네트워크 오류] 시도 {attempt}/{max_retries}: {req_err}")

        # 지수 백오프 대기
        if attempt < max_retries:
            wait_time = backoff_factor ** (attempt - 1)
            print(f"  [지수 백오프] {wait_time:.1f}초 대기 후 재시도합니다...")
            time.sleep(wait_time)

    print(f"  [최종 실패] {max_retries}회 재시도 후 요청 실패: {url}")
    return None


def extract_items_from_response(data: Optional[dict]) -> Tuple[List[dict], int]:
    """
    TourAPI 응답 본문에서 아이템 목록과 전체 건수(totalCount)를 안전하게 추출.
    데이터가 없거나 items가 빈 문자열("")인 경우를 방어적으로 처리.
    """
    if not data:
        return [], 0

    body = data.get("response", {}).get("body", {})
    try:
        total_count = int(body.get("totalCount", 0))
    except (ValueError, TypeError):
        total_count = 0

    items_wrapper = body.get("items", {})
    if not items_wrapper or not isinstance(items_wrapper, dict):
        return [], total_count

    items = items_wrapper.get("item", [])
    if isinstance(items, dict):
        return [items], total_count
    elif isinstance(items, list):
        return items, total_count
    return [], total_count


def map_tour_item(raw: dict) -> Optional[dict]:
    """
    TourAPI 원시 데이터를 Supabase `places` 테이블 DDL(V2.1) 스키마에 맞게 매핑 및 타입 변환.
    - 매핑 규칙:
      * contentid -> content_id (PK)
      * contenttypeid -> content_type_id
      * title -> title
      * addr1 -> address (+ addr2 상세주소 결합)
      * zipcode -> zipcode
      * tel -> tel
      * mapy -> latitude (float 변환)
      * mapx -> longitude (float 변환)
      * firstimage -> first_image
      * firstimage2 -> first_image2
      * areacode -> area_code
      * sigungucode -> sigungu_code
      * cat1, cat2, cat3 -> cat1, cat2, cat3
      * modifiedtime -> modified_time
      * is_active = TRUE
    """
    content_id = raw.get("contentid")
    if not content_id:
        return None
    content_id_str = str(content_id).strip()
    if not content_id_str:
        return None

    def clean_str(val: Any) -> Optional[str]:
        if val is None:
            return None
        s = str(val).strip()
        return s if s else None

    # 위도(latitude) 및 경도(longitude) float 변환
    mapy = raw.get("mapy")
    mapx = raw.get("mapx")
    lat: Optional[float] = None
    lon: Optional[float] = None

    if mapy is not None and str(mapy).strip():
        try:
            lat = float(str(mapy).strip())
        except (ValueError, TypeError):
            lat = None

    if mapx is not None and str(mapx).strip():
        try:
            lon = float(str(mapx).strip())
        except (ValueError, TypeError):
            lon = None

    # 주소 정제: addr1 -> address (상세주소 addr2가 있을 경우 결합)
    addr1 = clean_str(raw.get("addr1"))
    addr2 = clean_str(raw.get("addr2"))
    if addr1 and addr2:
        address = f"{addr1} {addr2}".strip()
    else:
        address = addr1

    return {
        "content_id": content_id_str,
        "content_type_id": clean_str(raw.get("contenttypeid")),
        "title": clean_str(raw.get("title")) or "무제",
        "address": address,
        "zipcode": clean_str(raw.get("zipcode")),
        "tel": clean_str(raw.get("tel")),
        "latitude": lat,
        "longitude": lon,
        "first_image": clean_str(raw.get("firstimage")),
        "first_image2": clean_str(raw.get("firstimage2")),
        "area_code": clean_str(raw.get("areacode")),
        "sigungu_code": clean_str(raw.get("sigungucode")),
        "cat1": clean_str(raw.get("cat1")),
        "cat2": clean_str(raw.get("cat2")),
        "cat3": clean_str(raw.get("cat3")),
        "modified_time": clean_str(raw.get("modifiedtime")),
        "is_active": True,
    }


def upsert_places_batch(
    supabase_client: Client,
    records: List[dict],
    batch_size: int = 300,
) -> int:
    """
    Supabase `places` 테이블에 content_id 기준으로 멱등 적재(Upsert) 수행.
    대량 데이터 처리 시 네트워크 및 페이로드 한계를 고려하여 배치 단위로 분할 전송.
    """
    if not records:
        return 0

    total_upserted = 0
    total_records = len(records)

    for i in range(0, total_records, batch_size):
        chunk = records[i : i + batch_size]
        res = (
            supabase_client.table("places")
            .upsert(chunk, on_conflict="content_id")
            .execute()
        )
        count = len(res.data) if res.data else len(chunk)
        total_upserted += count

    return total_upserted


def bulk_load_initial_data(
    supabase_client: Client,
    area_code: str = "1",
    target_types: Optional[List[str]] = None,
    num_of_rows: int = 1000,
) -> int:
    """
    (1) 베이스 데이터 벌크 수집 (Initial Bulk Load)
    - 엔드포인트: KorService2/areaBasedList2
    - 타겟 지역: 서울 (areaCode=1)
    - 타겟 타입: 관광지(12), 숙박(32), 음식점(39)
    - 파라미터: numOfRows=1000, pageNo 페이징 처리, MobileOS='ETC', MobileApp='FitBike', _type='json'
    - 타임아웃 10초 설정 및 실패 시 지수 백오프(최대 3회 재시도)
    """
    if target_types is None:
        target_types = ["12", "32", "39"]

    print("================================================================================")
    print(f" [TASK-003] 베이스 데이터 벌크 수집 시작 (지역코드: {area_code}, 타입: {target_types})")
    print("================================================================================")

    grand_total_upserted = 0

    for content_type_id in target_types:
        type_name = CONTENT_TYPE_NAMES.get(content_type_id, content_type_id)
        print(f"\n▶ [{type_name} (contentTypeId={content_type_id})] 수집 진행 중...")

        page_no = 1
        type_total_items = 0
        type_upserted_count = 0

        while True:
            params = {
                "serviceKey": TOUR_API_KEY,
                "numOfRows": num_of_rows,
                "pageNo": page_no,
                "MobileOS": "ETC",
                "MobileApp": "FitBike",
                "_type": "json",
                "areaCode": area_code,
                "contentTypeId": content_type_id,
            }

            data = fetch_api_with_retry(AREA_BASED_LIST_URL, params=params, max_retries=3, base_timeout=10.0)
            if not data:
                print(f"  [경고] {page_no}페이지 수집 실패로 인해 해당 타입 수집을 중단합니다.")
                break

            raw_items, total_count = extract_items_from_response(data)
            if not raw_items:
                print(f"  - {page_no}페이지: 수집된 아이템 없음. 완료.")
                break

            # 스키마 매핑 및 유효성 검증
            mapped_records = []
            for raw in raw_items:
                mapped = map_tour_item(raw)
                if mapped:
                    mapped_records.append(mapped)

            # Supabase 멱등 적재 (Upsert)
            upserted = upsert_places_batch(supabase_client, mapped_records, batch_size=300)
            type_upserted_count += upserted
            type_total_items += len(raw_items)

            print(f"  - {page_no}페이지: {len(raw_items)}건 수신 (매핑/적재: {upserted}건 / 누적 {type_total_items}/{total_count}건)")

            # 페이징 탈출 조건: 전체 건수에 도달했거나 수신 건수가 요청 건수보다 적은 경우
            if type_total_items >= total_count or len(raw_items) < num_of_rows:
                break

            page_no += 1

        print(f"  ✔ [{type_name}] 벌크 적재 완료: 총 {type_upserted_count}건 반영")
        grand_total_upserted += type_upserted_count

    print("\n--------------------------------------------------------------------------------")
    print(f"▶ 벌크 수집 전체 완료: 총 {grand_total_upserted}건 적재 처리됨")
    print("--------------------------------------------------------------------------------")
    return grand_total_upserted


def sync_daily_cdc(
    supabase_client: Client,
    modified_date: str,
    num_of_rows: int = 1000,
) -> Tuple[int, int]:
    """
    (3) 일일 증분 동기화 함수 (Daily CDC Sync)
    - 엔드포인트: KorService2/areaBasedSyncList2
    - 파라미터: modifiedtime={YYYYMMDD}, showflag=1 또는 0
    - showflag == '1': `places` 테이블에 최신 정보 Upsert
    - showflag == '0': 해당 content_id의 `is_active`를 FALSE로 업데이트 (폐업/비표출 처리)
    """
    print("================================================================================")
    print(f" [TASK-003] 일일 증분 동기화(CDC Sync) 시작 (수정일자 기준: {modified_date})")
    print("================================================================================")

    # 1. showflag == '1': 신규 및 변경 데이터 갱신 (Upsert)
    print("\n▶ [CDC 1단계] showflag=1 (신규/수정 관광지 최신 정보 Upsert)")
    page_no = 1
    total_upserted = 0
    accumulated_count = 0

    while True:
        params = {
            "serviceKey": TOUR_API_KEY,
            "numOfRows": num_of_rows,
            "pageNo": page_no,
            "MobileOS": "ETC",
            "MobileApp": "FitBike",
            "_type": "json",
            "showflag": "1",
            "modifiedtime": modified_date,
        }

        data = fetch_api_with_retry(AREA_BASED_SYNC_URL, params=params, max_retries=3, base_timeout=10.0)
        if not data:
            print(f"  [경고] showflag=1 {page_no}페이지 수집 실패.")
            break

        raw_items, total_count = extract_items_from_response(data)
        if not raw_items:
            print(f"  - showflag=1 대상 데이터 없음 (총 건수: {total_count})")
            break

        mapped_records = []
        for raw in raw_items:
            mapped = map_tour_item(raw)
            if mapped:
                mapped["is_active"] = True
                mapped_records.append(mapped)

        upserted = upsert_places_batch(supabase_client, mapped_records, batch_size=300)
        total_upserted += upserted
        accumulated_count += len(raw_items)

        print(f"  - {page_no}페이지: {len(raw_items)}건 수신 / {upserted}건 Upsert 완료 (진척도: {accumulated_count}/{total_count})")

        if accumulated_count >= total_count or len(raw_items) < num_of_rows:
            break
        page_no += 1

    print(f"  ✔ showflag=1 동기화 완료: 총 {total_upserted}건 갱신")

    # 2. showflag == '0': 삭제/폐업/비표출 관광지 비활성화 (is_active = FALSE)
    print("\n▶ [CDC 2단계] showflag=0 (폐업/비표출 관광지 is_active=FALSE 처리)")
    page_no = 0
    total_deactivated = 0
    accumulated_deact_count = 0

    while True:
        page_no += 1
        params = {
            "serviceKey": TOUR_API_KEY,
            "numOfRows": num_of_rows,
            "pageNo": page_no,
            "MobileOS": "ETC",
            "MobileApp": "FitBike",
            "_type": "json",
            "showflag": "0",
            "modifiedtime": modified_date,
        }

        data = fetch_api_with_retry(AREA_BASED_SYNC_URL, params=params, max_retries=3, base_timeout=10.0)
        if not data:
            print(f"  [경고] showflag=0 {page_no}페이지 수집 실패.")
            break

        raw_items, total_count = extract_items_from_response(data)
        if not raw_items:
            print(f"  - showflag=0 대상 데이터 없음 (총 건수: {total_count})")
            break

        target_ids = [str(item["contentid"]).strip() for item in raw_items if item.get("contentid")]
        accumulated_deact_count += len(raw_items)

        # ID 청크 단위로 places 테이블 is_active = False 일괄 업데이트
        now_utc = datetime.now(timezone.utc).isoformat()
        chunk_size = 200
        for i in range(0, len(target_ids), chunk_size):
            id_chunk = target_ids[i : i + chunk_size]
            upd_res = (
                supabase_client.table("places")
                .update({"is_active": False, "updated_at": now_utc})
                .in_("content_id", id_chunk)
                .execute()
            )
            deactivated_count = len(upd_res.data) if upd_res.data else 0
            total_deactivated += deactivated_count

        print(f"  - {page_no}페이지: {len(raw_items)}건 수신 중 DB 내 {total_deactivated}건 비활성화 완료 (진척도: {accumulated_deact_count}/{total_count})")

        if accumulated_deact_count >= total_count or len(raw_items) < num_of_rows:
            break

    print(f"  ✔ showflag=0 비활성화 완료: 총 {total_deactivated}건 비활성화 처리됨")
    print("--------------------------------------------------------------------------------")
    print(f"▶ CDC 동기화 완료 요약: Upsert {total_upserted}건, 비활성화 {total_deactivated}건")
    print("--------------------------------------------------------------------------------\n")
    return total_upserted, total_deactivated


def verify_and_print_smoke_test(supabase_client: Client) -> None:
    """
    스모크 테스트 완료 조건(DoD) 검증:
    - places 테이블 총 레코드 건수 조회
    - 최소 500건 이상 적재 검증
    - 샘플 3건의 로그를 터미널에 상세 출력
    """
    print("\n================================================================================")
    print("               [스모크 테스트 검증 결과 보고서 (DoD 검증)]")
    print("================================================================================")

    # 1. 총 레코드 건수 확인
    count_res = supabase_client.table("places").select("content_id", count="exact").execute()
    total_count = count_res.count if count_res.count is not None else len(count_res.data or [])

    print(f"▶ 1. Supabase 'places' 테이블 총 반영 건수: {total_count:,}건")

    if total_count >= 500:
        print(f"   [PASS] 완료 조건(DoD: 최소 500건 이상 적재)을 충족하였습니다! ({total_count} >= 500)")
    else:
        print(f"   [경고] 적재 건수({total_count}건)가 최소 기준 500건에 미달합니다.", file=sys.stderr)

    # 2. 샘플 레코드 3건 조회 및 터미널 출력
    print("\n▶ 2. DB 반영 샘플 데이터 (상위 3건):")
    sample_res = supabase_client.table("places").select("*").limit(3).execute()
    samples = sample_res.data or []

    if not samples:
        print("   [경고] 조회된 샘플 데이터가 없습니다.")
        return

    for idx, item in enumerate(samples, 1):
        type_name = CONTENT_TYPE_NAMES.get(str(item.get("content_type_id")), item.get("content_type_id"))
        print(f"\n  [샘플 #{idx}]")
        print(f"    • content_id      : {item.get('content_id')}")
        print(f"    • title           : {item.get('title')}")
        print(f"    • content_type    : {type_name} ({item.get('content_type_id')})")
        print(f"    • address         : {item.get('address')}")
        print(f"    • coordinates     : lat={item.get('latitude')}, lon={item.get('longitude')}")
        print(f"    • tel             : {item.get('tel') or '없음'}")
        print(f"    • area/sigungu    : {item.get('area_code')} / {item.get('sigungu_code')}")
        print(f"    • cat1/cat2/cat3  : {item.get('cat1')} > {item.get('cat2')} > {item.get('cat3')}")
        print(f"    • modified_time   : {item.get('modified_time')}")
        print(f"    • is_active       : {item.get('is_active')}")
        print(f"    • first_image     : {item.get('first_image') or '없음'}")

    print("\n================================================================================")


def main():
    parser = argparse.ArgumentParser(description="TourAPI 4.4 벌크 수집 및 일일 증분 동기화(CDC) 파이프라인")
    parser.add_argument(
        "--mode",
        choices=["bulk", "cdc", "all"],
        default="bulk",
        help="실행 모드 선택: 'bulk' (서울 지역 베이스 벌크 수집), 'cdc' (일일 증분 동기화), 'all' (벌크 및 CDC 순차 실행)",
    )
    parser.add_argument(
        "--area-code",
        type=str,
        default="1",
        help="벌크 수집 타겟 지역 코드 (기본값: 1 - 서울)",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="CDC 동기화 기준 수정일자 (YYYYMMDD 형식, 기본값: 당일 날짜)",
    )
    args = parser.parse_args()

    # 실행 모드에 따른 분기
    if args.mode in ["bulk", "all"]:
        bulk_load_initial_data(
            supabase_client=supabase,
            area_code=args.area_code,
            target_types=["12", "32", "39"],
            num_of_rows=1000,
        )

    if args.mode in ["cdc", "all"]:
        cdc_date = args.date or datetime.now().strftime("%Y%m%d")
        sync_daily_cdc(
            supabase_client=supabase,
            modified_date=cdc_date,
            num_of_rows=1000,
        )

    # 최종 결과 스모크 테스트 검증 및 로그 출력
    verify_and_print_smoke_test(supabase)


if __name__ == "__main__":
    main()
