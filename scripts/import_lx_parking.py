"""Geocode LX parking CSV and idempotently merge into existing parking_lots.

--dry-run performs geocoding and writes a review report without DB writes.
"""
import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from .import_parking_lots import ROOT, decode_csv, load_env, request_json
except ImportError:
    from import_parking_lots import ROOT, decode_csv, load_env, request_json


def read_lx(path):
    text, encoding = decode_csv(path)
    reader = csv.DictReader(io.StringIO(text))
    if not {'부서명', '주소', '주차장 이용료', '주차장 운영시간'}.issubset(reader.fieldnames or []):
        raise ValueError('Missing LX CSV columns')
    return [{key: (value or '').strip() for key, value in row.items()} for row in reader], encoding


def address_queries(address):
    original = ' '.join(address.split())
    cleaned = re.sub(r'\([^)]*\)', '', original).strip()
    # Some source rows append the institution name directly after a road number.
    cleaned = re.split(r'한국국토정보공사', cleaned)[0].strip()
    # The CSV concatenates building numbers and floor/room suffixes without spaces.
    without_floor = re.sub(r'\d(?:,\d)*층.*$', '', cleaned).strip()
    without_room = re.sub(r'\d{3}호.*$', '', without_floor).strip()
    return list(dict.fromkeys(query for query in (original, cleaned, without_floor, without_room) if query))


class Geocoder:
    def __init__(self, key, cache_path, delay=0.1):
        self.key, self.delay = key, delay
        self.cache = sqlite3.connect(cache_path)
        self.cache.execute('create table if not exists geocodes(address text primary key, lat real, lng real)')

    def lookup(self, address):
        for query in address_queries(address):
            cached = self.cache.execute('select lat,lng from geocodes where address=?', (query,)).fetchone()
            if cached:
                return cached
            for attempt in range(3):
                time.sleep(self.delay)
                try:
                    req = Request('https://dapi.kakao.com/v2/local/search/address.json?' + urlencode({'query': query}),
                                  headers={'Authorization': f'KakaoAK {self.key}'})
                    with urlopen(req, timeout=15) as response:
                        documents = json.load(response)['documents']
                    break
                except HTTPError as exc:
                    if exc.code in (401, 403, 429):
                        raise RuntimeError(f'Kakao HTTP {exc.code}; resume using cached coordinates') from None
                    if exc.code < 500 or attempt == 2:
                        raise RuntimeError(f'Kakao HTTP {exc.code}') from None
                except (URLError, TimeoutError):
                    if attempt == 2:
                        raise RuntimeError('Kakao network failure; rerun to resume') from None
                time.sleep(2 ** attempt)
            if not documents:
                continue
            lat, lng = float(documents[0]['y']), float(documents[0]['x'])
            if not (math.isfinite(lat) and math.isfinite(lng) and 33 <= lat <= 39 and 124 <= lng <= 132):
                raise ValueError('Kakao returned coordinates outside Korea')
            self.cache.execute('insert or replace into geocodes values(?,?,?)', (query, lat, lng))
            self.cache.commit()
            return lat, lng
        return None


def normalize(row, coordinates):
    address, department = row['주소'], row['부서명']
    identity = hashlib.sha256(f'{department}|{address}'.encode()).hexdigest()[:24]
    free = row['주차장 이용료'] == '무료'
    return dict(parking_no='LX-' + identity, name=f'LX {department} 주차장', type_se='LX',
                parking_type='부설', road_address=address, lot_address='', capacity=0,
                oper_days='현장 확인', weekday_time=row['주차장 운영시간'] or '운영시간 미제공',
                weekend_time='개방 여부 현장 확인', is_free=free,
                basic_fee=0 if free else None, basic_time=None, add_unit_time=None, add_unit_fee=None,
                daily_max_fee=None, discount_info=' '.join(filter(None, [row.get('기타'),
                    '평일 업무시간용 개방 주차장으로 현장 상황에 따라 이용이 제한될 수 있습니다.',
                    '2023-08-29 기준 자료로 방문 전 운영시간과 무료 개방 여부를 확인하세요.'])),
                tel=row.get('전화번호') or None, latitude=coordinates[0], longitude=coordinates[1])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', type=Path, default=ROOT / 'docs/한국국토정보공사 주차장 정보 DB_20230829.csv')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/lx-parking')
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    load_env()
    key = os.environ.get('KAKAO_REST_API_KEY')
    if not key:
        raise RuntimeError('KAKAO_REST_API_KEY is required')
    rows, encoding = read_lx(args.file)
    args.output.mkdir(parents=True, exist_ok=True)
    geocoder = Geocoder(key, args.output / 'geocodes.sqlite3')
    records, unresolved, matched = {}, [], 0
    try:
        for index, row in enumerate(rows[:args.limit], 1):
            coords = geocoder.lookup(row['주소'])
            if coords:
                matched += 1
                record = normalize(row, coords)
                records[record['parking_no']] = record
            else:
                unresolved.append(row)
            print(f'Geocoded {index}/{min(len(rows), args.limit or len(rows))}', flush=True)
    finally:
        geocoder.cache.close()
    report = dict(encoding=encoding, source_rows=len(rows), geocoded=len(records), matched_rows=matched,
                  duplicate_rows=matched - len(records), unresolved=len(unresolved),
                  dry_run=args.dry_run, upserted=0)
    (args.output / 'unresolved.json').write_text(json.dumps(unresolved, ensure_ascii=False, indent=2), encoding='utf8')
    (args.output / 'records.json').write_text(json.dumps(list(records.values()), ensure_ascii=False, indent=2), encoding='utf8')
    if not args.dry_run:
        url, secret = os.environ.get('SUPABASE_URL'), os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
        if not url or not secret:
            raise RuntimeError('SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY required')
        values = list(records.values())
        for offset in range(0, len(values), 20):
            chunk = values[offset:offset + 20]
            request_json(url.rstrip('/') + '/rest/v1/parking_lots?on_conflict=parking_no', secret, chunk,
                         prefer='resolution=merge-duplicates,return=minimal')
            report['upserted'] += len(chunk)
    (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf8')
    print(json.dumps(report))
    return 1 if unresolved else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Network exception URLs may contain credentials. Only report safe exception type.
        print(f'LX import stopped ({type(exc).__name__}); cached coordinates preserved.')
        raise SystemExit(1)
