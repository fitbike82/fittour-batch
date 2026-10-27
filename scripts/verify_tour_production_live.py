"""Bounded live production validation: 25 restaurants + 25 lodgings; no notifications.
Uses the same catalog mapper, page journal and transactional enrichment RPC as production.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import threading
from urllib.parse import unquote

import requests
from dotenv import dotenv_values
try:
    from sync_places_universe import fetch_page, map_item
    from sync_tour_enrichment_full import Database, Journal, TourClient, PRODUCTION_ENDPOINT_LIMIT
except ModuleNotFoundError:
    from scripts.sync_places_universe import fetch_page, map_item
    from scripts.sync_tour_enrichment_full import Database, Journal, TourClient, PRODUCTION_ENDPOINT_LIMIT


class RecordedHttp:
    def __init__(self, journal):
        self.local = threading.local()
        self.events = []
        self.lock = threading.Lock()
        self.journal = journal

    def get(self, url, **kwargs):
        endpoint = url.rsplit('/', 1)[-1]
        if endpoint == 'areaBasedList2':
            self.journal.reserve('KorService2', endpoint)
        if not hasattr(self.local, 'session'):
            self.local.session = requests.Session()
        response = self.local.session.get(url, **kwargs)
        with self.lock:
            self.events.append({'endpoint': endpoint, 'status': response.status_code,
                                'content_id': kwargs['params'].get('contentId')})
        return response


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    parser.add_argument('--directory', type=Path, default=Path('artifacts/tour-production-validation'))
    args = parser.parse_args(argv)
    env = dotenv_values(args.env_file)
    api_key = env.get('TOUR_API_DECODING_KEY') or env.get('TOUR_API_KEY')
    db_key = env.get('SUPABASE_SERVICE_ROLE_KEY') or env.get('SUPABASE_KEY')
    if not all((api_key, db_key, env.get('SUPABASE_URL'))):
        raise ValueError('Missing validation environment configuration')
    api_key = unquote(api_key.strip())
    db = Database(env['SUPABASE_URL'], db_key)
    if db.rpc('tour_enrichment_preflight', {}) is not True:
        raise ValueError('Enrichment preflight failed before spending API quota')
    args.directory.mkdir(parents=True, exist_ok=True)
    journal = Journal(str(args.directory / 'checkpoint.sqlite3'), api_key)
    http = RecordedHttp(journal)
    client = TourClient(api_key, journal, delay=0.1)
    client.http = http
    report = {'sample_size': 50, 'categories': {}, 'endpoints': [], 'errors': []}
    try:
        selected = []
        for category in (39, 32):
            rows, total = fetch_page(http, api_key, 'areaBasedList2', {'pageNo': 1, 'contentTypeId': category})
            if len(rows) < 25:
                raise ValueError('Provider returned fewer than 25 sample places')
            sample = [map_item(row, True) for row in rows[:25]]
            if any(str(row['content_type_id']) != str(category) for row in sample):
                raise ValueError('Provider category mismatch')
            selected.extend(sample)
            report['categories'][str(category)] = {'selected': len(sample), 'provider_total': total}
        ids = [str(row['content_id']) for row in selected]
        if len(set(ids)) != 50:
            raise ValueError('Duplicate sample place IDs')
        report['master_upsert'] = db.rpc('sync_places_universe_batch', {'p_rows': selected})
        for place in selected:
            journal.revision(place['content_id'], place['modified_time'])
        def collect(place, endpoint):
            pages, items = client.collect('KorService2', endpoint, place)
            return place, endpoint, pages, items
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(collect, place, endpoint) for place in selected
                       for endpoint in ('detailCommon2', 'detailIntro2', 'detailImage2')]
            for future in as_completed(futures):
                try:
                    place, endpoint, pages, items = future.result()
                    # DB writes remain serial and transactional to protect JSON/image upserts.
                    db.rpc('upsert_tour_enrichment', {'p_place_id': place['content_id'], 'p_service': 'KorService2',
                        'p_endpoint': endpoint, 'p_pages': pages, 'p_items': items, 'p_revision': place['modified_time']})
                    digest = hashlib.sha256(json.dumps(pages, sort_keys=True).encode()).hexdigest()
                    journal.mark(place['content_id'], 'KorService2', endpoint, digest)
                    report['endpoints'].append({'content_id': place['content_id'], 'endpoint': endpoint, 'pages': len(pages), 'items': len(items)})
                except Exception as exc:
                    report['errors'].append({'error_type': type(exc).__name__})
        ids_filter = 'in.(' + ','.join(ids) + ')'
        masters = db.request('GET', 'places', params={'select': 'content_id,content_type_id', 'content_id': ids_filter})
        stored = db.request('GET', 'place_tour_payloads', params={'select': 'place_id,endpoint,item_count,source_modified_time',
                            'place_id': ids_filter, 'source_service': 'eq.KorService2'})
        expected = {(row['content_id'], endpoint, row['modified_time']) for row in selected
                    for endpoint in ('detailCommon2', 'detailIntro2', 'detailImage2')}
        actual = {(row['place_id'], row['endpoint'], row['source_modified_time']) for row in stored}
        images = db.request('GET', 'place_images', params={'select': 'place_id', 'place_id': ids_filter, 'limit': 10000})
        db_categories = dict(Counter(str(row['content_type_id']) for row in masters))
        report.update(db_master_count=len(masters), db_categories=db_categories, matching_payloads=len(expected & actual), images_visible=len(images),
                      http_counts=dict(Counter(event['endpoint'] for event in http.events)), http_events=http.events,
                      endpoint_daily_limit=PRODUCTION_ENDPOINT_LIMIT)
        ok = len(masters) == 50 and db_categories == {'39': 25, '32': 25} and expected <= actual and not report['errors'] and all(e['status'] == 200 for e in http.events)
        report['passed'] = ok
        (args.directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({key: value for key, value in report.items() if key not in ('endpoints', 'http_events')}, ensure_ascii=False))
        return 0 if ok else 1
    finally:
        journal.db.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('Live validation failed: ' + type(exc).__name__)
        raise SystemExit(1)
