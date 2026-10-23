"""Import nationwide standard parking CSV. Standard library only; credentials stay server-side.

Run: python scripts/import_parking_lots.py [--dry-run] [--file path]
Duplicate management numbers use the latest 데이터기준일자; ties use the last row.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def load_env():
    for filename in ('.env.local', '.env'):
        path = ROOT / filename
        if path.exists():
            for line in path.read_text(encoding='utf-8-sig').splitlines():
                key, sep, value = line.partition('=')
                if sep and key.strip() and not key.lstrip().startswith('#'):
                    os.environ.setdefault(key.strip(), value.strip().strip('\"\''))


def decode_csv(path):
    raw = Path(path).read_bytes()
    encodings = ('utf-8-sig',) if raw.startswith(b'\xef\xbb\xbf') else ('cp949', 'euc-kr', 'utf-8-sig')
    for encoding in encodings:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError('CSV encoding is not cp949, euc-kr or UTF-8')


def number(value, default=0):
    cleaned = str(value or '').strip().replace(',', '')
    if not cleaned or cleaned == '-':
        return default
    # Preserve only explicit whole numeric values; do not turn descriptive fees into invented prices.
    if not re.fullmatch(r'\d+(?:\.0+)?', cleaned):
        return default
    return int(float(cleaned))


def parse_rows(text):
    rows = {}
    dates = {}
    stats = dict(source_rows=0, invalid_coordinates=0, missing_identity=0, duplicate_ids=0)
    reader = csv.DictReader(io.StringIO(text))
    required = {'주차장관리번호', '주차장명', '위도', '경도', '요금정보'}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError('Missing required CSV columns')
    for row in reader:
        stats['source_rows'] += 1
        try:
            lat, lng = float(row['위도']), float(row['경도'])
            if not (math.isfinite(lat) and math.isfinite(lng) and 33 <= lat <= 39 and 124 <= lng <= 132):
                raise ValueError()
        except (ValueError, TypeError):
            stats['invalid_coordinates'] += 1
            continue
        def text(key):
            return (row.get(key) or '').strip()
        parking_no, name = text('주차장관리번호'), text('주차장명')
        if not parking_no or not name:
            stats['missing_identity'] += 1
            continue
        date = text('데이터기준일자')
        if parking_no in rows:
            stats['duplicate_ids'] += 1
            if date < dates[parking_no]:
                continue
        def hours(prefix):
            start, end = text(prefix + '운영시작시각'), text(prefix + '운영종료시각')
            return f'{start} ~ {end}' if start and end else ''
        dates[parking_no] = date
        rows[parking_no] = dict(
            parking_no=parking_no, name=name, type_se=text('주차장구분'), parking_type=text('주차장유형'),
            road_address=text('소재지도로명주소'), lot_address=text('소재지지번주소'), capacity=number(text('주차구획수')),
            oper_days=text('운영요일'), weekday_time=hours('평일'),
            weekend_time=f"토 {hours('토요일') or '미제공'} / 공휴일 {hours('공휴일') or '미제공'}",
            is_free='무료' in text('요금정보'), basic_time=number(text('주차기본시간')),
            basic_fee=number(text('주차기본요금')), add_unit_time=number(text('추가단위시간')),
            add_unit_fee=number(text('추가단위요금')), daily_max_fee=number(text('1일주차권요금'), None),
            discount_info=text('감면/할인정보') or text('특기사항'), tel=text('전화번호'), latitude=lat, longitude=lng,
        )
    stats.update(unique_valid_rows=len(rows), free_lots=sum(row['is_free'] for row in rows.values()))
    return list(rows.values()), stats


def request_json(url, key, data=None, method='POST', prefer=None):
    headers = {'apikey': key, 'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
    if prefer:
        headers['Prefer'] = prefer
    body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8') if data is not None else None
    for attempt in range(4):
        try:
            with urlopen(Request(url, data=body, headers=headers, method=method), timeout=90) as response:
                return response.headers
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise RuntimeError(f'Supabase HTTP {error.code}; batch was not confirmed') from None
        except URLError:
            if attempt == 3:
                raise RuntimeError('Supabase network failure; rerun safely to resume via upsert') from None
        time.sleep(2 ** attempt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', type=Path, default=ROOT / 'docs/전국주차장정보표준데이터.csv')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    text, encoding = decode_csv(args.file)
    rows, stats = parse_rows(text)
    stats['encoding'] = encoding
    print(json.dumps(stats, ensure_ascii=True), flush=True)
    if args.dry_run:
        return
    load_env()
    url = os.environ.get('SUPABASE_URL') or os.environ.get('NEXT_PUBLIC_SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        raise SystemExit('SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required')
    if not rows:
        raise SystemExit('No valid rows; import cancelled')
    endpoint = url.rstrip('/') + '/rest/v1/parking_lots'
    for offset in range(0, len(rows), 1000):
        batch = rows[offset:offset + 1000]
        request_json(endpoint + '?on_conflict=parking_no', key, batch, prefer='resolution=merge-duplicates,return=minimal')
        print(f'Upserted {offset + len(batch)}/{len(rows)}', flush=True)
    headers = request_json(endpoint + '?select=parking_no', key, method='HEAD', prefer='count=exact')
    stats.update(upserted=len(rows), database_count=headers.get('Content-Range', '').split('/')[-1])
    print(json.dumps(stats, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
