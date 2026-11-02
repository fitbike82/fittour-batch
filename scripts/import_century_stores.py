"""Match the 2022 public CSV to active places. Default is a reviewable dry run."""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import unicodedata

from dotenv import load_dotenv
from supabase import create_client, ClientOptions

try:
    from place_taxonomy import classify, CONFIG
except ModuleNotFoundError:
    from scripts.place_taxonomy import classify, CONFIG

CSV_PATH = Path('docs/소상공인시장진흥공단_전국 백년가게 현황_20220713.csv')
SOURCE = 'century_csv:20220713:' + CONFIG['version']
REGIONS = {'서울특별시': '서울', '부산광역시': '부산', '대구광역시': '대구', '인천광역시': '인천',
           '광주광역시': '광주', '대전광역시': '대전', '울산광역시': '울산', '세종특별자치시': '세종',
           '경기도': '경기', '강원특별자치도': '강원', '강원도': '강원', '충청북도': '충북',
           '충청남도': '충남', '전북특별자치도': '전북', '전라북도': '전북', '전라남도': '전남',
           '경상북도': '경북', '경상남도': '경남', '제주특별자치도': '제주', '제주도': '제주'}


def compact(value):
    return re.sub(r'[^0-9a-z가-힣]', '', unicodedata.normalize('NFKC', str(value or '')).lower())


def name_key(value):
    value = re.sub(r'주식회사|유한회사|\(주\)|㈜', '', str(value or ''))
    return compact(value)


def phones(value):
    result = set()
    for part in re.split(r'[,;/\n]', str(value or '')):
        number = re.sub(r'\D', '', part)
        if 8 <= len(number) <= 11:
            result.add(number)
    return result


def address_key(value):
    value = re.sub(r'\([^)]*\)', '', str(value or ''))
    for old, new in REGIONS.items():
        value = value.replace(old, new)
    return re.sub(r'[^0-9a-z가-힣-]', '', unicodedata.normalize('NFKC', value).lower())


def locality(record, address):
    province = REGIONS.get(record.get('시도', ''), record.get('시도', ''))
    district = compact(record.get('시군구'))
    key = address_key(address)
    return bool(province and district and key.startswith(compact(province)) and district in key)


def read_csv(path):
    raw = Path(path).read_bytes()
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = raw.decode('cp949')
    rows = list(csv.DictReader(io.StringIO(text, newline='')))
    required = {'연번', '업체명', '연락처', '시도', '시군구', '기본주소', '주요사업'}
    if not rows or not required.issubset(rows[0]):
        raise ValueError('CSV header mismatch')
    if any(not row['연번'].strip() for row in rows) or len({row['연번'] for row in rows}) != len(rows):
        raise ValueError('CSV serial numbers must be unique')
    return rows, hashlib.sha256(raw).hexdigest()


def match_records(records, places):
    by_name, by_phone = defaultdict(set), defaultdict(set)
    for index, place in enumerate(places):
        by_name[name_key(place.get('title'))].add(index)
        for phone in phones(place.get('tel')):
            by_phone[phone].add(index)
    events = []
    for record in records:
        name, numbers = name_key(record['업체명']), phones(record['연락처'])
        pool = set(by_name.get(name, ())) if name else set()
        for number in numbers:
            pool.update(by_phone.get(number, ()))
        candidates = []
        for index in sorted(pool):
            place = places[index]
            if place.get('is_active') is not True:
                continue
            same_name = bool(name and name == name_key(place.get('title')))
            same_phone = bool(numbers.intersection(phones(place.get('tel'))))
            region = locality(record, place.get('address') or place.get('addr1'))
            source_address, destination = address_key(record['기본주소']), address_key(place.get('address') or place.get('addr1'))
            same_address = bool(source_address and destination and source_address == destination)
            similarity = SequenceMatcher(None, name, name_key(place.get('title'))).ratio()
            reason, score = None, 0
            if same_name and same_phone and region:
                reason, score = 'name_phone_region', 100
            elif same_name and same_address and region:
                reason, score = 'name_exact_address', 95
            elif same_phone and region and min(len(name), len(name_key(place.get('title')))) >= 4 and similarity >= .92:
                reason, score = 'phone_region_similar_name', 90
            else:
                score = (40 if same_name else int(similarity * 20)) + (20 if region else 0) + (20 if same_phone else 0)
            candidates.append({'content_id': place['content_id'], 'title': place.get('title'),
                               'address': place.get('address') or place.get('addr1'), 'score': score,
                               'reason': reason, 'similarity': round(similarity, 3)})
        candidates.sort(key=lambda row: (-row['score'], str(row['content_id'])))
        best = candidates[0] if candidates else None
        confident = best and best['reason'] and (len(candidates) == 1 or best['score'] - candidates[1]['score'] >= 15)
        events.append({'csv_id': record['연번'], 'csv_name': record['업체명'],
                       'status': 'matched' if confident else 'review' if candidates else 'unmatched',
                       'content_id': best['content_id'] if confident else None,
                       'reason': best['reason'] if confident else 'ambiguous_or_weak' if candidates else 'no_candidate',
                       'candidates': candidates[:5]})
    # Do not let two CSV rows overwrite one place's certification provenance.
    collisions = Counter(row['content_id'] for row in events if row['status'] == 'matched')
    for row in events:
        if row['status'] == 'matched' and collisions[row['content_id']] > 1:
            row.update(status='review', content_id=None, reason='multiple_csv_rows_for_place')
    return events


def update_payload(record, place, digest, event):
    info = {'source': CSV_PATH.name, 'source_date': '2022-07-13', 'source_sha256': digest,
            'csv_id': record['연번'], 'name': record['업체명'], 'business': record['주요사업'],
            'address': record['기본주소'], 'phone': record['연락처'], 'match_reason': event['reason'],
            'matched_at': datetime.now(timezone.utc).isoformat(), 'current_certification_verified': False}
    payload = {'is_century_store': True, 'century_store_info': info}
    taxonomy = classify(place, record['주요사업'])
    source = place.get('taxonomy_source') or ''
    if taxonomy and (not source or source.startswith('century_csv:20220713:') or source.startswith('gemini:')):
        payload.update(taxonomy, taxonomy_source=SOURCE)
    payload['filter_tags'] = sorted(set(place.get('filter_tags') or []).union(payload.get('filter_tags', []), ['백년가게']))
    return payload


def conditional_update(db, place, payload):
    query = db.table('places').update(payload).eq('content_id', place['content_id']).eq('is_active', True)
    for field in ('modified_time', 'title', 'address', 'addr1', 'tel', 'taxonomy_source', 'category_l1', 'category_l2',
                  'is_century_store', 'filter_tags', 'century_store_info'):
        value = place.get(field)
        if isinstance(value, list):
            value = '{' + ','.join('"' + str(tag).replace('\\', '\\\\').replace('"', '\\"') + '"' for tag in value) + '}'
        elif isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False)
        query = query.is_(field, 'null') if value is None else query.eq(field, value)
    return bool(query.execute().data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', type=Path, default=CSV_PATH)
    parser.add_argument('--env-file', default='.env')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--resolve-review', action='store_true')
    parser.add_argument('--review-input', type=Path, default=Path('artifacts/century-stores/matching.json'))
    parser.add_argument('--unmatched', type=Path, default=Path('docs/unmatched_century_stores.json'))
    args = parser.parse_args()
    args.report = args.report or Path('artifacts/century-stores/relaxed_applied.json' if args.resolve_review
                                      else 'artifacts/century-stores/matching.json')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    load_dotenv(args.env_file)
    url, key = os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        raise ValueError('Missing server database configuration')
    records, digest = read_csv(args.csv)
    db = create_client(url, key, options=ClientOptions(postgrest_client_timeout=30))
    places, after = [], None
    while True:
        query = db.table('places').select('content_id,content_type_id,title,address,addr1,tel,is_active,modified_time,'
                                          'is_century_store,century_store_info,category_l1,category_l2,filter_tags,taxonomy_source') \
            .eq('is_active', True).order('content_id').limit(500)
        if after is not None:
            query = query.gt('content_id', after)
        page = query.execute().data or []
        if not page:
            break
        places.extend(page)
        after = page[-1]['content_id']
    if args.resolve_review:
        try:
            from resolve_century_reviews import resolve_run
        except ModuleNotFoundError:
            from scripts.resolve_century_reviews import resolve_run
        return resolve_run(args, db, records, digest, places)
    events = match_records(records, places)
    place_by_id = {place['content_id']: place for place in places}
    record_by_id = {record['연번']: record for record in records}
    report = {'source_rows': len(records), 'active_places': len(places), 'source_sha256': digest,
              'applied': 0, 'conflicts': 0, 'errors': 0, 'mode': 'apply' if args.apply else 'dry_run', 'events': events}
    args.report.parent.mkdir(parents=True, exist_ok=True)

    def save():
        report['counts'] = dict(Counter(event['status'] for event in events))
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    save()
    if args.apply:
        for event in events:
            if event['status'] != 'matched':
                continue
            place = place_by_id[event['content_id']]
            payload = update_payload(record_by_id[event['csv_id']], place, digest, event)
            try:
                # Guard the complete observed inputs, including concurrent taxonomy
                # updates: no stale matcher can overwrite a newly reviewed place.
                committed = conditional_update(db, place, payload)
                event['committed'] = committed
                report['applied' if committed else 'conflicts'] += 1
            except Exception as exc:
                event['write_error'] = str(getattr(exc, 'code', '') or type(exc).__name__)
                report['errors'] += 1
            save()
    print(json.dumps({key: value for key, value in report.items() if key != 'events'}, ensure_ascii=False, indent=2))
    return 1 if report['errors'] or report['conflicts'] else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('Century import failed: ' + str(getattr(exc, 'code', '') or type(exc).__name__), file=sys.stderr)
        raise SystemExit(1)
