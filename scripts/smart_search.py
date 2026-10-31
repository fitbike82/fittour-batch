"""Smart blog queries and environment-selected Naver -> Kakao adapters."""
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import requests


def parallel_review_pages(session, kakao_key, title, suffixes, size=5, sido=None, sigungu=None):
    """Each round searches Naver blog/cafe and Kakao concurrently, without fallback suppression."""
    bases = build_smart_queries(title, sido, sigungu)
    if not bases:
        return
    # Three bounded rounds; prefer the region-qualified query over a broad namesake search.
    region = ' '.join(dict.fromkeys(str(part).strip() for part in (sido, sigungu) if part))
    base = f'{region} {sanitize_place_title(title)}'.strip() if region else bases[0]
    queries = [f'{base} {suffix}' for suffix in suffixes]
    providers = []
    client_id, secret = os.getenv('NAVER_CLIENT_ID', '').strip(), os.getenv('NAVER_CLIENT_SECRET', '').strip()
    if client_id and secret:
        headers = {'X-NCP-APIGW-API-KEY-ID': client_id, 'X-NCP-APIGW-API-KEY': secret}
        for kind in ('blog', 'cafearticle'):
            providers.append(('naver', kind, f'https://naverapihub.apigw.ntruss.com/search/v1/{kind}', headers))
    key = kakao_key or os.getenv('KAKAO_REST_API_KEY') or os.getenv('KAKAO_API_KEY')
    if key:
        providers.append(('kakao', 'blog', 'https://dapi.kakao.com/v2/search/blog', {'Authorization': f'KakaoAK {key}'}))
    if not providers:
        raise ValueError('missing_search_configuration')

    def fetch(spec, query):
        provider, kind, endpoint, headers = spec
        # Production sessions are isolated per worker; never share mutable cookies/adapters.
        worker = (session.fork() if hasattr(session, 'fork') else requests.Session()) if isinstance(session, requests.Session) else None
        client = worker or session
        try:
            params = {'query': query, 'display' if provider == 'naver' else 'size': size,
                      'sort': 'sim' if provider == 'naver' else 'accuracy'}
            if provider == 'naver':
                params['format'] = 'json'
            response = client.get(endpoint, headers=headers, params=params, timeout=15)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError('invalid_search_response')
            rows = payload.get('items' if provider == 'naver' else 'documents')
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError('invalid_search_response')
            if provider == 'naver':
                rows = [{'title': row.get('title', ''), 'url': row.get('link', ''),
                         'contents': row.get('description', '')} for row in rows]
            return {'provider': provider, 'kind': kind, 'query': query, 'documents': rows[:size]}
        except (requests.RequestException, ValueError) as exc:
            response = getattr(exc, 'response', None)
            return {'provider': provider, 'kind': kind, 'query': query, 'documents': [],
                    'error': str(response.status_code) if response is not None else type(exc).__name__}
        finally:
            if worker is not None:
                worker.close()

    with ThreadPoolExecutor(max_workers=3) as executor:
        for query in queries:
            futures = [executor.submit(fetch, spec, query) for spec in providers]
            # Keep deterministic source ordering while all requests run concurrently.
            yield [future.result() for future in futures]


def sanitize_place_title(title):
    clean = unicodedata.normalize('NFKC', title)
    while True:
        updated = re.sub(r'\([^()]*\)|\[[^\[\]]*\]', ' ', clean)
        if updated == clean:
            break
        clean = updated
    clean = re.sub(r'주식회사|직영점|\d+호점|본점', ' ', clean)
    return re.sub(r'\s+', ' ', re.sub(r'[^a-zA-Z0-9가-힣\s]', ' ', clean)).strip()


def build_smart_queries(title, sido=None, sigungu=None):
    clean = sanitize_place_title(title)
    if not clean:
        return []
    region = sanitize_place_title((sigungu or '').strip() or (sido or '').strip())
    region = re.sub(r'(?:특별자치도|특별자치시|특별시|광역시|시|군|구|도)$', '', region).strip()
    return ([f'{region} {clean}'] if region and region not in clean else []) + [clean]


def has_search_credentials(kakao_key=None):
    return bool((os.getenv('NAVER_CLIENT_ID', '').strip() and os.getenv('NAVER_CLIENT_SECRET', '').strip())
                or (kakao_key or '').strip() or os.getenv('KAKAO_REST_API_KEY', '').strip()
                or os.getenv('KAKAO_API_KEY', '').strip())


def smart_search_pages(session, kakao_key, title, suffixes, size=10, sido=None, sigungu=None):
    """Yield normalized pages lazily so caller can stop after enough valid reviews.

    Exhaust Naver queries before Kakao. Provider errors skip to the next provider;
    if none could be queried, preserve the HTTP error for existing quota handling.
    """
    bases = build_smart_queries(title, sido, sigungu)
    if not bases:
        return
    queries = list(dict.fromkeys(f'{base} {suffix}'.strip() for base in bases for suffix in suffixes))
    providers = []
    client_id = os.getenv('NAVER_CLIENT_ID', '').strip()
    secret = os.getenv('NAVER_CLIENT_SECRET', '').strip()
    if client_id and secret:
        providers.append(('naver', 'https://naverapihub.apigw.ntruss.com/search/v1/blog',
                          {'X-NCP-APIGW-API-KEY-ID': client_id, 'X-NCP-APIGW-API-KEY': secret}))
    key = (kakao_key or '').strip() or os.getenv('KAKAO_REST_API_KEY', '').strip() or os.getenv('KAKAO_API_KEY', '').strip()
    if key:
        providers.append(('kakao', 'https://dapi.kakao.com/v2/search/blog', {'Authorization': f'KakaoAK {key}'}))
    if not providers:
        raise ValueError('missing_search_configuration')
    last_error = None
    for provider, endpoint, headers in providers:
        last_error = None
        try:
            for query in queries:
                params = {'query': query, 'display' if provider == 'naver' else 'size': 5 if provider == 'naver' else size,
                          'sort': 'sim' if provider == 'naver' else 'accuracy'}
                if provider == 'naver':
                    params['format'] = 'json'
                response = session.get(endpoint, headers=headers, params=params, timeout=15)
                if response is None:
                    return
                response.raise_for_status()
                payload = response.json()
                rows = payload.get('items' if provider == 'naver' else 'documents') if isinstance(payload, dict) else None
                if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                    raise ValueError('invalid_search_response')
                if provider == 'naver':
                    rows = [{'title': re.sub(r'<[^>]*>?', '', row.get('title') or ''), 'url': row.get('link', ''),
                             'contents': re.sub(r'<[^>]*>?', '', row.get('description') or ''),
                             'datetime': row.get('postdate', '')} for row in rows]
                yield query, rows
        except StopIteration:
            return
        except (requests.RequestException, ValueError) as error:
            last_error = error
            continue
    # Do not turn the final provider's quota/outage into a permanent empty result,
    # even if an earlier query returned an empty/insufficient page.
    if last_error is not None:
        raise last_error
