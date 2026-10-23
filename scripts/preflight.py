"""Read-only integration checks; no claims, writes, uploads or notifications."""
import json
import os
from pathlib import Path
from urllib.parse import unquote

import requests
from dotenv import load_dotenv
from supabase import create_client, ClientOptions


def main():
    load_dotenv()
    checks = {}
    def check(name, operation):
        try:
            operation()
            checks[name] = {'status': 'passed'}
        except Exception as exc:
            # Exception strings and request URLs can contain provider credentials.
            checks[name] = {'status': 'failed', 'error_type': type(exc).__name__}
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                checks[name]['http_status'] = exc.response.status_code
    required = ('SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY', 'TOUR_API_DECODING_KEY', 'KAKAO_REST_API_KEY', 'GEMINI_API_KEY')
    missing = [name for name in required if not os.environ.get(name, '').strip()]
    if missing:
        raise SystemExit('Missing secrets: ' + ', '.join(missing))
    db = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY'], options=ClientOptions(postgrest_client_timeout=20))
    for table, columns in (('places','content_id,is_active,needs_ai_summary'), ('place_ai_summaries','content_id,quality_status,source_reviews'), ('place_details','place_id'), ('parking_lots','parking_no'), ('place_tour_payloads','place_id')):
        check('supabase_' + table, lambda table=table, columns=columns: db.table(table).select(columns).limit(1).execute())
    def schema():
        if db.rpc('tour_enrichment_preflight', {}).execute().data is not True:
            raise ValueError('Schema preflight was not true')
    check('tour_schema', schema)
    def tour():
        response = requests.get('https://apis.data.go.kr/B551011/KorService2/areaBasedList2', params={'serviceKey': unquote(os.environ['TOUR_API_DECODING_KEY'].strip()), 'MobileOS': 'ETC', 'MobileApp': 'FitTour', '_type': 'json', 'numOfRows': 1, 'pageNo': 1, 'contentTypeId': '12', 'arrange': 'A'}, timeout=(10,30))
        response.raise_for_status()
        if str(response.json()['response']['header']['resultCode']) not in ('0000','00','0'):
            raise ValueError('TourAPI rejected the request')
    check('tour_api', tour)
    def kakao():
        response = requests.get('https://dapi.kakao.com/v2/search/blog', params={'query': '안동 여행', 'size': 1}, headers={'Authorization': 'KakaoAK ' + os.environ['KAKAO_REST_API_KEY']}, timeout=(10,20))
        response.raise_for_status()
        if 'documents' not in response.json():
            raise ValueError('Unexpected Kakao response')
    check('kakao_search', kakao)
    def gemini():
        response = requests.get('https://generativelanguage.googleapis.com/v1beta/models', headers={'x-goog-api-key': os.environ['GEMINI_API_KEY']}, timeout=(10,20))
        response.raise_for_status()
        if not response.json().get('models'):
            raise ValueError('No models returned')
    check('gemini_authentication', gemini)
    path = Path('artifacts/preflight/report.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(checks, indent=2), encoding='utf-8')
    print(json.dumps(checks, indent=2))
    return int(any(value['status'] != 'passed' for value in checks.values()))


if __name__ == '__main__':
    raise SystemExit(main())
