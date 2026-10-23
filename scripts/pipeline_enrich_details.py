"""
TASK-003-FIX: 상세 데이터 누락 및 이미지 WebP 스토리지 적재 파이프라인
- detailCommon2: overview 및 연락처(tel) 보강
- detailIntro2: 영업시간, 휴무일, 메뉴, 주차, 체크인/아웃 정보 파싱 후 place_details 적재
- 이미지 최적화: 외부 이미지 다운로드 -> Pillow 가로 800px 리사이징 & WebP 80% 변환 -> Supabase Storage places 버킷 적재
- 원칙 준수: 0원 인프라, 타입 안전성, 멱등성 (기변환 이미지 스킵, on_conflict Upsert)
"""

import os
import sys
import io
import re
import html
import time
import argparse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from PIL import Image
from supabase import create_client, Client

# Windows 콘솔 UTF-8 입출력 인코딩 보장
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass

# 1. 환경변수 로드
load_dotenv()

TOUR_API_KEY = os.getenv("TOUR_API_KEY") or os.getenv("TOUR_API_DECODING_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
# API 키 로드 (SERVICE_ROLE_KEY가 유효한 키 형식일 때만 사용, 아니면 SUPABASE_KEY 사용)
_srv_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
_anon_key = os.getenv("SUPABASE_KEY", "").strip()
if _srv_key and not _srv_key.startswith("http") and (_srv_key.startswith("sb") or _srv_key.startswith("ey")):
    SUPABASE_KEY = _srv_key
else:
    SUPABASE_KEY = _anon_key

if not all([TOUR_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    print("[오류] 필수 환경변수가 누락되었습니다.", file=sys.stderr)
    print(f"  - TOUR_API_KEY: {'OK' if TOUR_API_KEY else 'MISSING'}", file=sys.stderr)
    print(f"  - SUPABASE_URL: {'OK' if SUPABASE_URL else 'MISSING'}", file=sys.stderr)
    print(f"  - SUPABASE_KEY: {'OK' if SUPABASE_KEY else 'MISSING'}", file=sys.stderr)
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# TourAPI 4.4 엔드포인트
TOUR_API_BASE_URL = "https://apis.data.go.kr/B551011/KorService2"
DETAIL_COMMON_URL = f"{TOUR_API_BASE_URL}/detailCommon2"
DETAIL_INTRO_URL = f"{TOUR_API_BASE_URL}/detailIntro2"

STORAGE_BUCKET_NAME = "places"


def clean_html(raw_html: Optional[str]) -> Optional[str]:
    """
    HTML 태그 제거 및 줄바꿈/엔티티 정제.
    <br> -> \n 변환, 태그 제거, 다중 공백 단일화.
    """
    if not raw_html:
        return None
    text = str(raw_html)
    # <br> 태그를 줄바꿈으로 치환
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    # 일반 태그 제거
    text = re.sub(r"<[^>]+>", " ", text)
    # HTML 특수문자 디코딩 (&amp;, &lt;, &gt; 등)
    text = html.unescape(text)
    # 연속된 공백 및 줄바꿈 정리
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    cleaned = "\n".join([line for line in lines if line]).strip()
    return cleaned if cleaned else None


def fetch_api_with_retry(
    url: str,
    params: Dict[str, Any],
    max_retries: int = 3,
    base_timeout: float = 10.0,
    backoff_factor: float = 2.0,
) -> Optional[dict]:
    """
    타임아웃(10초) 및 지수 백오프(최대 3회 재시도)를 적용한 HTTP GET 함수.
    """
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, params=params, timeout=base_timeout)
            if response.status_code == 200:
                try:
                    data = response.json()
                except Exception as json_err:
                    if attempt < max_retries:
                        time.sleep(backoff_factor ** (attempt - 1))
                        continue
                    return None

                header = data.get("response", {}).get("header", {})
                result_code = header.get("resultCode")
                if result_code == "0000":
                    return data
                else:
                    result_msg = header.get("resultMsg", "Unknown Error")
                    # 파라미터 오류 등은 재시도해도 실패하므로 조기 반환
                    if result_code in ["10", "11", "20", "30"]:
                        return None
            else:
                pass
        except requests.exceptions.RequestException:
            pass

        if attempt < max_retries:
            time.sleep(backoff_factor ** (attempt - 1))

    return None


def fetch_detail_common(content_id: str) -> Optional[dict]:
    """
    TourAPI KorService2/detailCommon2 호출하여 overview, tel 등 공통 상세 정보 수집.
    """
    params = {
        "serviceKey": TOUR_API_KEY,
        "MobileOS": "ETC",
        "MobileApp": "FitBike",
        "_type": "json",
        "contentId": str(content_id),
    }
    data = fetch_api_with_retry(DETAIL_COMMON_URL, params=params, max_retries=3, base_timeout=10.0)
    if not data:
        return None

    items = data.get("response", {}).get("body", {}).get("items", {})
    if isinstance(items, dict):
        item_list = items.get("item", [])
        if isinstance(item_list, dict):
            return item_list
        elif isinstance(item_list, list) and item_list:
            return item_list[0]
    return None


def fetch_detail_intro(content_id: str, content_type_id: str) -> Optional[dict]:
    """
    TourAPI KorService2/detailIntro2 호출하여 타입별 세부 소개 정보(영업시간, 휴무일, 메뉴 등) 수집.
    """
    params = {
        "serviceKey": TOUR_API_KEY,
        "MobileOS": "ETC",
        "MobileApp": "FitBike",
        "_type": "json",
        "contentId": str(content_id),
        "contentTypeId": str(content_type_id),
    }
    data = fetch_api_with_retry(DETAIL_INTRO_URL, params=params, max_retries=3, base_timeout=10.0)
    if not data:
        return None

    items = data.get("response", {}).get("body", {}).get("items", {})
    if isinstance(items, dict):
        item_list = items.get("item", [])
        if isinstance(item_list, dict):
            return item_list
        elif isinstance(item_list, list) and item_list:
            return item_list[0]
    return None


def ensure_places_bucket(supabase_client: Client) -> bool:
    """
    Supabase Storage 'places' 버킷 존재 여부 확인 및 자동 생성 시도.
    """
    try:
        # 버킷 내 목록 조회 시도로 존재 여부 검사
        supabase_client.storage.from_(STORAGE_BUCKET_NAME).list(limit=1)
        return True
    except Exception as e:
        err_msg = str(e)
        if "Bucket not found" in err_msg or "404" in err_msg:
            try:
                supabase_client.storage.create_bucket(STORAGE_BUCKET_NAME, options={"public": True})
                print(f"✔ Supabase Storage '{STORAGE_BUCKET_NAME}' 공개 버킷이 성공적으로 생성되었습니다.")
                return True
            except Exception as create_err:
                print(f"\n[안내] Supabase Storage '{STORAGE_BUCKET_NAME}' 버킷 생성이 필요합니다:")
                print(f"  - 원인: {create_err}")
                print("  - 조치: Supabase 대시보드 -> Storage -> 'New bucket' -> Name: 'places', Public: 'ON' 으로 생성하거나,")
                print("         .env 파일에 SUPABASE_SERVICE_ROLE_KEY를 추가해 주세요.\n")
                return False
        return True


def process_image_to_webp(
    image_url: str,
    content_id: str,
    supabase_client: Client,
    max_width: int = 800,
    quality: int = 80,
) -> Optional[str]:
    """
    외부 이미지 다운로드 -> Pillow WebP 변환 (품질 80%, 가로 최대 800px) -> Supabase Storage places 버킷 업로드.
    이미 Supabase Storage URL인 경우 변환을 건너뛰어 멱등성 보장.
    """
    if not image_url or not str(image_url).strip():
        return None

    clean_url = str(image_url).strip()

    # 이미 Supabase Storage URL인 경우 스킵
    storage_prefix = f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/public/{STORAGE_BUCKET_NAME}/"
    if clean_url.startswith(storage_prefix):
        return clean_url

    try:
        # 1. 이미지 메모리 다운로드
        res = requests.get(clean_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if res.status_code != 200 or not res.content:
            return None

        # 2. Pillow 이미지 열기 및 RGB 변환
        with Image.open(io.BytesIO(res.content)) as img:
            if img.mode in ("RGBA", "LA", "P"):
                # 투명 배경인 경우 흰색 배경 합성 후 RGB 변환
                bg = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                bg.paste(img, mask=img.split()[-1] if "A" in img.mode else None)
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")

            # 3. 가로 최대 800px 리사이징 (종횡비 유지)
            orig_w, orig_h = img.size
            if orig_w > max_width:
                new_w = max_width
                new_h = int(orig_h * (max_width / orig_w))
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            # 4. WebP 품질 80% 인코딩
            webp_buffer = io.BytesIO()
            img.save(webp_buffer, format="WEBP", quality=quality, method=4)
            webp_bytes = webp_buffer.getvalue()

        # 5. Supabase Storage 업로드 ({content_id}.webp)
        storage_path = f"{content_id}.webp"
        supabase_client.storage.from_(STORAGE_BUCKET_NAME).upload(
            path=storage_path,
            file=webp_bytes,
            file_options={"content-type": "image/webp", "upsert": "true"},
        )

        # 6. 공개 Storage URL 생성
        public_url = supabase_client.storage.from_(STORAGE_BUCKET_NAME).get_public_url(storage_path)
        return public_url

    except Exception as img_err:
        print(f"    [이미지 처리 실패] ({content_id}): {img_err}")
        return None


def parse_place_details_payload(content_id: str, content_type_id: str, intro_raw: dict) -> dict:
    """
    detailIntro2 원시 응답을 Supabase place_details 테이블 스키마에 맞게 정제.
    - place_id (PK/FK)
    - business_hours (영업/이용시간)
    - day_off (휴무일/쉬는날)
    - parking_available (주차 정보)
    - chk_credit_card (신용카드 결제 가능 여부)
    - first_menu, treat_menu (음식점 대표/취급 메뉴)
    - check_in_time, check_out_time (숙박 체크인/아웃)
    - raw_details (전체 원본 보존용)
    - updated_at
    """
    business_hours = None
    day_off = None
    parking = None
    credit_card = None
    first_menu = None
    treat_menu = None
    check_in = None
    check_out = None

    if content_type_id == "39":  # 음식점
        business_hours = clean_html(intro_raw.get("opentimefood"))
        day_off = clean_html(intro_raw.get("restdatefood"))
        parking = clean_html(intro_raw.get("parkingfood"))
        credit_card = clean_html(intro_raw.get("chkcreditcardfood"))
        first_menu = clean_html(intro_raw.get("firstmenu"))
        treat_menu = clean_html(intro_raw.get("treatmenu"))
    elif content_type_id == "12":  # 관광지
        business_hours = clean_html(intro_raw.get("usetime"))
        day_off = clean_html(intro_raw.get("restdate"))
        parking = clean_html(intro_raw.get("parking"))
        credit_card = clean_html(intro_raw.get("chkcreditcard"))
    elif content_type_id == "32":  # 숙박
        check_in = clean_html(intro_raw.get("checkintime"))
        check_out = clean_html(intro_raw.get("checkouttime"))
        parking = clean_html(intro_raw.get("parkinglodging"))
        credit_card = clean_html(intro_raw.get("chkcreditcard"))

    now_utc = datetime.now(timezone.utc).isoformat()

    return {
        "place_id": str(content_id),
        "business_hours": business_hours,
        "day_off": day_off,
        "parking_available": parking,
        "chk_credit_card": credit_card,
        "first_menu": first_menu,
        "treat_menu": treat_menu,
        "check_in_time": check_in,
        "check_out_time": check_out,
        "raw_details": intro_raw,
        "updated_at": now_utc,
    }


def enrich_place_record(
    place: dict,
    supabase_client: Client,
    bucket_ready: bool,
) -> Tuple[bool, bool, bool]:
    """
    단일 관광지 레코드에 대해:
    1) detailCommon2 조회 -> places.overview 업데이트
    2) detailIntro2 조회 -> place_details 테이블 Upsert
    3) first_image WebP 변환 및 Supabase Storage 적재 -> places.first_image 업데이트
    """
    content_id = str(place["content_id"])
    content_type_id = str(place.get("content_type_id") or "12")
    title = place.get("title", "")
    existing_img = place.get("first_image")

    overview_updated = False
    details_upserted = False
    image_converted = False

    # 1. detailCommon2 호출
    common_data = fetch_detail_common(content_id)
    overview_text = clean_html(common_data.get("overview")) if common_data else None
    tel_text = clean_html(common_data.get("tel")) if common_data else None

    # places 테이블 업데이트 페이로드 구성
    places_update: Dict[str, Any] = {}
    now_utc = datetime.now(timezone.utc).isoformat()
    places_update["updated_at"] = now_utc

    if overview_text:
        places_update["overview"] = overview_text
        overview_updated = True

    if tel_text and not place.get("tel"):
        places_update["tel"] = tel_text

    # 2. 이미지 WebP 변환 및 Storage 업로드
    if bucket_ready and existing_img:
        storage_url = process_image_to_webp(
            image_url=existing_img,
            content_id=content_id,
            supabase_client=supabase_client,
        )
        if storage_url and storage_url != existing_img:
            places_update["first_image"] = storage_url
            image_converted = True

    # places 테이블 업데이트 실행
    if len(places_update) > 1:  # updated_at 외 변경 사항이 있을 때
        supabase_client.table("places").update(places_update).eq("content_id", content_id).execute()

    # 3. detailIntro2 호출 및 place_details Upsert
    intro_data = fetch_detail_intro(content_id, content_type_id)
    if intro_data:
        details_payload = parse_place_details_payload(content_id, content_type_id, intro_data)
        supabase_client.table("place_details").upsert(details_payload, on_conflict="place_id").execute()
        details_upserted = True

    return overview_updated, details_upserted, image_converted


def run_pipeline(limit: int = 30):
    print("================================================================================")
    print(f" [TASK-003-FIX] 상세 데이터 누락 및 이미지 WebP 스토리지 적재 파이프라인 (처리 대상: {limit}건)")
    print("================================================================================")

    # 1. 스토리지 버킷 준비 확인
    bucket_ready = ensure_places_bucket(supabase)

    # 2. 대상 데이터 조회 (first_image가 있는 항목 우선 또는 상위 N건)
    print(f"\n▶ Supabase 'places' 테이블에서 상위 {limit}건을 추출합니다...")
    # 이미지 교체 테스트 효과 극대화를 위해 first_image가 존재하는 관광지를 우선 선별
    query_res = (
        supabase.table("places")
        .select("content_id, content_type_id, title, address, first_image, overview, tel")
        .not_.is_("first_image", "null")
        .order("content_id", desc=False)
        .limit(limit)
        .execute()
    )
    targets = query_res.data or []

    # first_image 있는 대상이 부족한 경우 전체 상위에서 추가 보충
    if len(targets) < limit:
        existing_ids = {t["content_id"] for t in targets}
        remaining_limit = limit - len(targets)
        query_res2 = (
            supabase.table("places")
            .select("content_id, content_type_id, title, address, first_image, overview, tel")
            .order("content_id", desc=False)
            .limit(remaining_limit * 2)
            .execute()
        )
        for r in (query_res2.data or []):
            if r["content_id"] not in existing_ids:
                targets.append(r)
                if len(targets) >= limit:
                    break

    print(f"▶ 대상 추출 완료: 총 {len(targets)}건 선정됨\n")

    # 3. 순회 처리
    success_overview = 0
    success_details = 0
    success_images = 0

    for idx, place in enumerate(targets, 1):
        cid = place["content_id"]
        title = place.get("title", "무제")
        ctype = place.get("content_type_id", "12")
        print(f"[{idx:02d}/{len(targets):02d}] 처리 중: [{cid}] {title} (타입: {ctype})")

        ov_ok, dt_ok, img_ok = enrich_place_record(place, supabase, bucket_ready)

        if ov_ok:
            success_overview += 1
        if dt_ok:
            success_details += 1
        if img_ok:
            success_images += 1

        status_str = f"개요(overview)={'✔' if ov_ok else 'SKIP'}, 세부정보(place_details)={'✔' if dt_ok else 'SKIP'}, WebP스토리지={'✔' if img_ok else 'SKIP'}"
        print(f"     └─ 결과: {status_str}")

        # API 과부하 방지 최소 딜레이 (0.1초)
        time.sleep(0.1)

    print("\n--------------------------------------------------------------------------------")
    print(f"▶ 파이프라인 처리 요약: 총 {len(targets)}건 중")
    print(f"   • places.overview 소개글 반영     : {success_overview}건")
    print(f"   • place_details 상세 테이블 적재  : {success_details}건")
    print(f"   • WebP 변환 및 Storage URL 교체   : {success_images}건")
    print("--------------------------------------------------------------------------------")

    # 4. DoD 검증 및 샘플 3건 터미널 상세 출력
    verify_and_print_results(supabase, targets[:3])


def verify_and_print_results(supabase_client: Client, sample_targets: List[dict]):
    print("\n================================================================================")
    print("               [TASK-003-FIX 스모크 테스트 최종 검증 보고서]")
    print("================================================================================")

    sample_ids = [t["content_id"] for t in sample_targets]

    # places 데이터 조회
    places_res = supabase_client.table("places").select("*").in_("content_id", sample_ids).execute()
    places_map = {p["content_id"]: p for p in (places_res.data or [])}

    # place_details 데이터 조회
    details_res = supabase_client.table("place_details").select("*").in_("place_id", sample_ids).execute()
    details_map = {d["place_id"]: d for d in (details_res.data or [])}

    for idx, cid in enumerate(sample_ids, 1):
        p = places_map.get(cid, {})
        d = details_map.get(cid, {})

        print(f"\n[검증 샘플 #{idx}] content_id: {cid} | {p.get('title')}")
        print("  1. places 테이블 검증:")
        overview_preview = (p.get('overview') or '')[:120] + ('...' if len(p.get('overview') or '') > 120 else '')
        print(f"     • overview (소개글) : {overview_preview if overview_preview else '[NULL]'}")
        print(f"     • first_image (URL) : {p.get('first_image')}")
        print(f"     • tel (전화번호)    : {p.get('tel') or '없음'}")

        print("  2. place_details 테이블 검증:")
        if d:
            print(f"     • business_hours    : {d.get('business_hours') or '정보 없음'}")
            print(f"     • day_off (휴무일)  : {d.get('day_off') or '정보 없음'}")
            print(f"     • first_menu (대표) : {d.get('first_menu') or '해당 없음'}")
            print(f"     • treat_menu (취급) : {d.get('treat_menu') or '해당 없음'}")
            print(f"     • parking_available : {d.get('parking_available') or '정보 없음'}")
            print(f"     • check_in/out      : {d.get('check_in_time') or '-'} / {d.get('check_out_time') or '-'}")
            print(f"     • credit_card       : {d.get('chk_credit_card') or '정보 없음'}")
        else:
            print("     • [경고] place_details 적재 내역 없음")

    print("\n================================================================================")


def main():
    parser = argparse.ArgumentParser(description="TASK-003-FIX 상세 데이터 인리치먼트 및 이미지 WebP 스토리지 적재")
    parser.add_argument(
        "--limit",
        type=int,
        default=30,
        help="처리할 대상 레코드 건수 (기본값: 30건 스모크 테스트)",
    )
    args = parser.parse_args()
    run_pipeline(limit=args.limit)


if __name__ == "__main__":
    main()
