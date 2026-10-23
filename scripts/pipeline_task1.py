import os
import sys
from datetime import datetime
from dotenv import load_dotenv
import requests
from supabase import create_client, Client

load_dotenv()

TOUR_API_KEY = os.getenv("TOUR_API_DECODING_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not all([TOUR_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    print("Error: Missing required environment variables in .env.", file=sys.stderr)
    sys.exit(1)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

TOUR_API_URL = "https://apis.data.go.kr/B551011/KorService2/areaBasedList2"

def fetch_tour_data(num_of_rows: int = 100, page_no: int = 1, l_dong_regn_cd: str = "11", content_type_id: str = "12"):
    params = {
        "serviceKey": TOUR_API_KEY,
        "numOfRows": num_of_rows,
        "pageNo": page_no,
        "MobileOS": "ETC",
        "MobileApp": "LocalTripAI",
        "_type": "json",
        "arrange": "C",
        "contentTypeId": content_type_id,
        "lDongRegnCd": l_dong_regn_cd,
    }
    
    response = requests.get(TOUR_API_URL, params=params, timeout=15)
    
    if not response.ok:
        print(f"[API Error] HTTP {response.status_code}")
        print(f"Details: {response.text}")
        return []

    try:
        data = response.json()
    except Exception as e:
        print(f"[Parsing Error] Failed to parse JSON response: {e}")
        print(f"Raw Response: {response.text[:200]}")
        return []

    # Check for OpenAPI Service Error header
    header = data.get("response", {}).get("header", {})
    if not header:
        svc_resp = data.get("OpenAPI_ServiceResponse", {})
        if svc_resp:
            cmm_header = svc_resp.get("cmmMsgHeader", {})
            err_msg = cmm_header.get("errMsg")
            reason_code = cmm_header.get("returnReasonCode")
            print(f"[TourAPI Gateway Error] Code: {reason_code}, Msg: {err_msg}")
            return []

    items = data.get("response", {}).get("body", {}).get("items", {})
    if not items:
        return []
    
    item_list = items.get("item", [])
    if isinstance(item_list, dict):
        item_list = [item_list]
    return item_list

def parse_and_filter_item(raw: dict):
    # 1. Coordinate Boundary check (33.0 <= lat <= 38.6, 124.0 <= lon <= 132.0)
    mapy = raw.get("mapy")
    mapx = raw.get("mapx")
    if not mapy or not mapx:
        return None
    
    try:
        lat = float(mapy)
        lng = float(mapx)
    except (ValueError, TypeError):
        return None
        
    if not (33.0 <= lat <= 38.6 and 124.0 <= lng <= 132.0):
        return None

    # 2. Image URL normalization (firstimage -> firstimage2 fallback -> None)
    first_image = raw.get("firstimage") or raw.get("firstimage2")
    if not first_image or str(first_image).strip() == "":
        first_image = None
    else:
        first_image = str(first_image).strip()

    # 3. Address normalization
    addr1 = raw.get("addr1", "").strip() if raw.get("addr1") else ""
    addr2 = raw.get("addr2", "").strip() if raw.get("addr2") else ""
    address = f"{addr1} {addr2}".strip() if addr1 else None

    # 4. Tel normalization
    tel = raw.get("tel")
    tel = str(tel).strip() if tel and str(tel).strip() else None

    # 5. Modified time parsing
    mod_time_raw = raw.get("modifiedtime")
    mod_time = None
    if mod_time_raw:
        try:
            mod_time = datetime.strptime(str(mod_time_raw), "%Y%m%d%H%M%S").isoformat()
        except Exception:
            mod_time = None

    # 6. ID & Codes
    content_id = raw.get("contentid")
    if not content_id:
        return None

    category_id = str(raw.get("contenttypeid", "12"))
    area_code_val = str(raw.get("lDongRegnCd") or raw.get("areacode") or "11")

    return {
        "content_id": str(content_id),
        "title": raw.get("title", "").strip(),
        "category_id": category_id,
        "address": address,
        "tel": tel,
        "first_image": first_image,
        "latitude": lat,
        "longitude": lng,
        "area_code": area_code_val,
        "modified_time": mod_time,
    }

def main():
    print("[TASK-001] Starting TourAPI v4.4 (KorService2) data collection...")
    raw_items = fetch_tour_data(num_of_rows=100, page_no=1, l_dong_regn_cd="11", content_type_id="12")
    print(f"TourAPI fetch count: {len(raw_items)}")

    if not raw_items:
        print("[Warning] No data retrieved from TourAPI.")
        return

    records = []
    skipped = 0
    for raw in raw_items:
        parsed = parse_and_filter_item(raw)
        if parsed:
            records.append(parsed)
        else:
            skipped += 1

    print(f"Validated records: {len(records)} | Skipped (out-of-bounds/missing): {skipped}")

    if not records:
        print("No valid records to load into DB.")
        return

    # Idempotent upsert via content_id
    res = supabase.table("places").upsert(records, on_conflict="content_id").execute()
    inserted_count = len(res.data) if res.data else 0
    print(f"[Supabase] Successfully upserted {inserted_count} records into 'places'.")

if __name__ == "__main__":
    main()
