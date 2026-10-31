"""Two-pass review queue: python batch_ai_summary_cron.py --limit 50."""
import argparse
from collections import Counter
import json
import logging
import os
import re
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

try:
    from smart_search import parallel_review_pages, has_search_credentials
except ModuleNotFoundError:
    from scripts.smart_search import parallel_review_pages, has_search_credentials

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from supabase import create_client, ClientOptions
from postgrest.exceptions import APIError
try:
    from review_evidence import basic_summary
except ModuleNotFoundError:
    from scripts.review_evidence import basic_summary

from pipeline_task2 import (
    Gemini, MODEL, MODEL_CHAIN, GeminiQuotaDeferred, GeminiCircuitOpen,
    GeminiDailyQuotaExhausted, clean_html, sponsored, review_excerpt,
    mask_sensitive, setup_masking,
)

try:
    from gemini_key_pool import discover_keys
except ModuleNotFoundError:
    from scripts.gemini_key_pool import discover_keys

LOG = logging.getLogger(__name__)
EXCLUDED_REASONS = ('insufficient_evidence', 'No unsponsored readable reviews',
                    'Invalid ', 'Ungrounded ')


class TimeBudgetExpired(RuntimeError):
    pass


class ReviewCollectionUnavailable(RuntimeError):
    pass


class BudgetSession(requests.Session):
    def __init__(self, deadline=None, clock=time.monotonic):
        super().__init__()
        self.deadline, self.clock = deadline, clock
        self.headers['User-Agent'] = 'LocalTripAI/1.0'

    def fork(self):
        return BudgetSession(self.deadline, self.clock)

    def request(self, method, url, **kwargs):
        if self.deadline is not None:
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                raise TimeBudgetExpired('Batch time budget reached')
            kwargs['timeout'] = min(kwargs.get('timeout', 15), remaining)
        return super().request(method, url, **kwargs)


def budget_sleep(seconds, deadline, sleep=time.sleep, clock=time.monotonic):
    if clock() + seconds > deadline:
        raise TimeBudgetExpired('Batch time budget reached')
    sleep(seconds)


def read_article(session, url):
    # Reuse validated blog URL handling; remove sponsored paragraphs before slicing.
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(url)
    host = parsed.hostname or ''
    if parsed.scheme not in ('http', 'https'):
        raise ValueError('Unsupported article URL')
    if host in ('blog.naver.com', 'm.blog.naver.com'):
        query = parse_qs(parsed.query)
        if query.get('blogId') and query.get('logNo'):
            url = f"https://m.blog.naver.com/{query['blogId'][0]}/{query['logNo'][0]}"
        else:
            url = 'https://m.blog.naver.com' + parsed.path
    elif host in ('cafe.naver.com', 'm.cafe.naver.com'):
        # Fetch public HTML only. No login/session bypass or snippet substitution.
        url = 'https://m.cafe.naver.com' + parsed.path + ('?' + parsed.query if parsed.query else '')
    elif not (host.endswith('.tistory.com') or host == 'brunch.co.kr'):
        raise ValueError('Unsupported article host')
    response = session.get(url, timeout=15, allow_redirects=False)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    for element in soup.select('script,style,noscript,nav,header,footer'):
        element.decompose()
    container = soup.select_one('.se-main-container, #postViewArea, .tt_article_useless_p_margin, #article-view, .entry-content, .wrap_body, .ContentRenderer, .article_viewer, #postContent')
    if container is None:
        raise ValueError('Cafe public body unavailable' if 'cafe.naver.com' in host else 'Unreadable article')
    paragraphs = [p.strip() for p in container.get_text('\n', strip=True).splitlines() if p.strip()]
    # A disclosed sponsored post is dropped entirely: removing the disclosure alone
    # does not turn the remaining review into an independent opinion.
    if any(sponsored(p) for p in paragraphs):
        raise ValueError('Sponsored article')
    text = ' '.join(paragraphs)
    if len(text) < 150:
        raise ValueError('Article too short')
    return review_excerpt(text, limit=3000)


def search_reviews(session, key, title, *, sido=None, sigungu=None):
    reviews, seen = [], set()
    diagnostics = {'queries': 0, 'documents': 0, 'article_errors': {}, 'filtered': {},
                   'search_errors': {}, 'sources': {'naver': 0, 'kakao': 0}, 'body_characters': 0,
                   'returned_documents': 0, 'duplicates': 0, 'region_available': bool(sido or sigungu)}
    session.review_diagnostics = diagnostics
    unavailable = 0
    pages = parallel_review_pages(session, key, title, ('방문 후기', '솔직 후기', '내돈내산'),
                                  size=5, sido=sido, sigungu=sigungu)
    for round_pages in pages:
        # Interleave Naver blog and cafe so neither monopolizes the five Naver slots.
        candidates = []
        for index in range(5):
            for page in round_pages:
                if index < len(page['documents']):
                    candidates.append((page, page['documents'][index]))
        for page in round_pages:
            diagnostics['queries'] += 1
            diagnostics['returned_documents'] += len(page['documents'])
            if page.get('error'):
                code = page['provider'] + '_' + page['kind'] + '_' + page['error']
                diagnostics['search_errors'][code] = diagnostics['search_errors'].get(code, 0) + 1
        for page, doc in candidates:
            provider = page['provider']
            if diagnostics['sources'][provider] >= 5:
                continue
            diagnostics['documents'] += 1
            url = doc.get('url', '')
            if not url or url in seen:
                diagnostics['duplicates'] += 1
                continue
            seen.add(url)
            heading = clean_html(doc.get('title'))
            if sponsored(heading + ' ' + clean_html(doc.get('contents'))):
                diagnostics['filtered']['sponsored'] = diagnostics['filtered'].get('sponsored', 0) + 1
                continue
            try:
                text = read_article(session, url)
            except requests.RequestException as exc:
                code = str(exc.response.status_code) if exc.response is not None else type(exc).__name__
                diagnostics['article_errors'][code] = diagnostics['article_errors'].get(code, 0) + 1
                # Deleted/unavailable individual articles must not discard other reviews.
                # Preserve temporary collection failure if too few alternatives remain.
                if code not in ('404', '410'):
                    unavailable += 1
                LOG.warning('article_host=%s stage=read_article error_code=%s', urlparse(url).hostname, code)
                continue
            except ValueError as exc:
                reason = str(exc)
                diagnostics['filtered'][reason] = diagnostics['filtered'].get(reason, 0) + 1
                continue
            reviews.append({'source_id': len(reviews) + 1, 'title': heading,
                            'url': url, 'query': page['query'], 'text': text,
                            'provider': provider, 'kind': page['kind'],
                            'published_at': doc.get('datetime'),
                            'fetched_at': datetime.now(timezone.utc).isoformat()})
            diagnostics['sources'][provider] += 1
            diagnostics['body_characters'] += len(text)
        if all(diagnostics['sources'][provider] >= 5 for provider in {p['provider'] for p in round_pages}):
            break
    diagnostics['readable'] = len(reviews)
    diagnostics['target'] = 10
    diagnostics['shortfall'] = 10 - len(reviews)
    diagnostics['missing_providers'] = sorted({'naver', 'kakao'} - {p['provider'] for p in round_pages}) if diagnostics['queries'] else ['naver', 'kakao']
    diagnostics['collection_reason'] = ('complete' if len(reviews) == 10 else
        'partial_sources' if reviews else 'no_search_results' if not diagnostics['returned_documents']
        else 'no_usable_body')
    if len(reviews) < 2 and (unavailable or diagnostics['search_errors']):
        raise ReviewCollectionUnavailable('Insufficient sources after temporary article failures')
    return reviews


def review_region(db, place):
    # Queue RPCs may omit addresses. Fetch only when needed; never treat numeric area codes as names.
    address = place.get('addr1') or place.get('address')
    sido, sigungu = place.get('sido'), place.get('sigungu')
    if not address and not (sido or sigungu) and place.get('claim_token'):
        rows = db.table('places').select('addr1,address').eq('content_id', place['content_id']).limit(1).execute().data or []
        if isinstance(rows, list) and rows:
            address = rows[0].get('addr1') or rows[0].get('address')
    parts = str(address or '').split()
    if not sido and parts:
        sido = parts[0]
    if not sigungu and len(parts) > 1:
        sigungu = parts[1]
    return sido, sigungu


def exclusion_category(reason):
    if reason == 'No unsponsored readable reviews':
        return 'no_usable_body'
    if reason == 'insufficient_evidence':
        return 'insufficient_evidence'
    if reason and reason.startswith('Ungrounded '):
        return 'quote_validation'
    if reason and reason.startswith('Invalid '):
        return 'output_validation'
    return reason or 'unknown'


def pending_places(db, limit):
    # Retry only explicit rolled-back transactions. A lost response could have
    # committed leases, so transport errors must not blindly claim another batch.
    for attempt in range(4):
        try:
            return db.rpc('claim_ai_review_work', {'batch_limit': limit}).execute().data or []
        except APIError as exc:
            if getattr(exc, 'code', None) not in ('57014', '40001', '40P01') or attempt == 3:
                raise
            LOG.warning('stage=claim database_retry=%s code=%s', attempt + 1, safe_error_code(exc))
            time.sleep(2 ** attempt)


def finish_queue(db, place):
    query = db.table('places').update({'needs_ai_summary': False}) \
        .eq('content_id', place['content_id']).eq('needs_ai_summary', True).eq('is_active', True)
    revision = place.get('modified_time')
    query = query.is_('modified_time', 'null') if revision is None else query.eq('modified_time', revision)
    return bool(query.execute().data)


def persist(db, place, summary, reviews, status, model, reason=None, diagnostics=None):
    if place.get('claim_token'):
        return bool(db.rpc('complete_ai_review_work', {
            'job_content_id': place['content_id'], 'claim_token': place['claim_token'],
            'input_modified_time': place.get('modified_time'), 'result_status': status,
            'summary_data': summary, 'model_name': model, 'reason_code': reason,
            'reviews_data': [{k: r[k] for k in ('source_id', 'title', 'url', 'query', 'provider', 'kind', 'published_at', 'fetched_at') if k in r} for r in reviews],
            'diagnostics_data': diagnostics or {},
        }).execute().data)
    payload = {'content_id': place['content_id'], 'quality_status': status, 'model': model,
        'honest_summary': '\n'.join(summary['summary_lines']) if summary else None,
        'pros': summary['pros'] if summary else [], 'cons': summary['cons'] if summary else [],
        'visit_tips': summary['visit_tips'] if summary else None,
        'source_reviews': {'input_modified_time': place.get('modified_time'), 'reason': reason,
            'analysis_basis': (summary or {}).get('analysis_basis', 'review_analysis'),
            'analyzed_at': datetime.now(timezone.utc).isoformat(),
            'reviews': [{k: r[k] for k in ('source_id', 'title', 'url', 'query', 'provider', 'kind', 'published_at', 'fetched_at') if k in r} for r in reviews]},
        'updated_at': datetime.now(timezone.utc).isoformat()}
    db.table('place_ai_summaries').upsert(payload, on_conflict='content_id').execute()
    # If the place changed during generation, leave the newly queued revision pending.
    return finish_queue(db, place)


def resume_saved(db, place):
    # Claimed work is committed atomically. A retry must regenerate insufficient
    # results instead of treating last pass's saved rejection as a new result.
    if place.get('claim_token'):
        return None
    rows = db.table('place_ai_summaries').select('quality_status,source_reviews') \
        .eq('content_id', place['content_id']).limit(1).execute().data or []
    if not rows:
        return None
    row = rows[0]
    provenance = row.get('source_reviews')
    if isinstance(provenance, dict) and 'input_modified_time' in provenance \
            and provenance['input_modified_time'] == place.get('modified_time') \
            and row.get('quality_status') in ('ready', 'insufficient'):
        return row['quality_status'] if finish_queue(db, place) else None
    return None


class SystemCAAdapter(HTTPAdapter):
    """Use Windows/system trust roots without disabling TLS verification."""
    def init_poolmanager(self, *args, **kwargs):
        context = ssl.create_default_context()
        if sys.platform.startswith('win'):
            # Python 3.13+ enables X509_STRICT, rejecting otherwise trusted
            # Windows enterprise roots that omit AKI. Keep chain + hostname checks.
            context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs['ssl_context'] = context
        return super().init_poolmanager(*args, **kwargs)


def notify(message):
    token, chat = os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat:
        LOG.warning('Telegram configuration missing')
        return False
    try:
        with requests.Session() as session:
            session.mount('https://', SystemCAAdapter())
            response = session.post(f'https://api.telegram.org/bot{token}/sendMessage',
                                    json={'chat_id': chat, 'text': message}, timeout=15)
            response.raise_for_status()
            return response.json().get('ok') is True
    except Exception as exc:
        LOG.warning('Telegram failed: %s', type(exc).__name__)
        return False


def safe_error_code(exc):
    if isinstance(exc, ReviewCollectionUnavailable):
        return 'review_collection_unavailable'
    if isinstance(exc, APIError):
        code = str(getattr(exc, 'code', '') or '')
        return 'database_' + (code if re.fullmatch(r'[A-Z0-9]{5,12}', code) else 'unknown')
    if isinstance(exc, requests.RequestException):
        return f'HTTP_{exc.response.status_code}' if exc.response is not None else mask_sensitive(type(exc).__name__)
    if isinstance(exc, RuntimeError) and str(exc).startswith('Gemini request failed ('):
        return mask_sensitive(str(exc))  # Gemini emits a controlled class/status message, never request URLs.
    return mask_sensitive(type(exc).__name__)


def write_report(path, counts):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    exclusions = Counter((e.get('diagnostics', {}).get('collection_reason', 'no_usable_body')
                          if e.get('reason') == 'No unsponsored readable reviews' else exclusion_category(e.get('reason')))
                         for e in counts.get('events', []) if e.get('status') == 'insufficient')
    failures = Counter(e.get('reason', 'unknown') for e in counts.get('events', []) if e.get('status') == 'error')
    filtered, article_errors, search_errors = Counter(), Counter(), Counter()
    for event in counts.get('events', []):
        diagnostics = event.get('diagnostics') or {}
        filtered.update(diagnostics.get('filtered', {}))
        article_errors.update(diagnostics.get('article_errors', {}))
        search_errors.update(diagnostics.get('search_errors', {}))
    path.write_text(json.dumps({'checked_at': datetime.now(timezone.utc).isoformat(), **counts,
                               'exclusion_reasons': dict(exclusions), 'failure_reasons': dict(failures),
                               'body_filter_reasons': dict(filtered), 'article_errors': dict(article_errors),
                               'search_errors': dict(search_errors)},
                               ensure_ascii=False, indent=2), encoding='utf-8')


def run(db, session, generator, kakao_key, limit, sleep=time.sleep, clock=time.monotonic, report_path=None):
    started = clock()
    counts = {'processed': 0, 'success': 0, 'excluded': 0, 'errors': 0, 'deferred': 0, 'quota_deferred': 0, 'quota_stopped': False, 'places_started': 0,
              'basic_info': 0, 'review_analysis': 0}
    places = pending_places(db, limit)
    counts.update(selected=len(places), pass_number=places[0].get('attempt_number', 1) if places else None,
                  events=[], release_errors=0)
    try:
        search_429_streak = 0
        for index, place in enumerate(places):
            request_events = getattr(generator, 'request_events', [])
            event_start = len(request_events) if isinstance(request_events, list) else 0
            counts['places_started'] += 1
            session.review_diagnostics = {}
            try:
                resumed = resume_saved(db, place)
                reason = None
                if resumed:
                    status = resumed
                    completed = True
                else:
                    sido, sigungu = review_region(db, place)
                    reviews = search_reviews(session, kakao_key, place['title'], sido=sido, sigungu=sigungu)
                    session.review_diagnostics['region_available'] = bool(sido or sigungu)
                    search_429_streak = 0
                    try:
                        summary, model = generator.summarize({**place, 'sido': sido, 'sigungu': sigungu}, reviews)
                        status = 'ready'
                    except ValueError as exc:
                        reason = str(exc)
                        if not reason.startswith(EXCLUDED_REASONS):
                            raise
                        actual_model = getattr(generator, 'last_model', None)
                        if reason in ('No unsponsored readable reviews', 'insufficient_evidence'):
                            summary, model, status = basic_summary(place, reason), None, 'ready'
                        else:
                            summary, model, status = None, actual_model if isinstance(actual_model,str) else MODEL, 'insufficient'
                    session.review_diagnostics['analysis_basis'] = (summary or {}).get('analysis_basis', 'review_analysis')
                    session.review_diagnostics['checked_at'] = datetime.now(timezone.utc).isoformat()
                    validation = getattr(generator, 'validation_events', [])
                    session.review_diagnostics['validation_failures'] = validation if isinstance(validation, list) else []
                    session.review_diagnostics['repair_attempted'] = getattr(generator, 'repair_attempted', False) is True
                    completed = persist(db, place, summary, reviews, status, model, reason,
                                        diagnostics=session.review_diagnostics)
                    if completed and status == 'ready':
                        counts[session.review_diagnostics['analysis_basis']] += 1
                counts['processed'] += 1
                if completed:
                    counts['success' if status == 'ready' else 'excluded'] += 1
                else:
                    counts['deferred'] += 1
                counts['events'].append({'content_id': place['content_id'], 'status': status,
                    'attempt_number': place.get('attempt_number', 1), 'reason': reason,
                    'committed': completed, 'diagnostics': session.review_diagnostics})
                LOG.info('content_id=%s pass=%s status=%s committed=%s reason=%s',
                         place['content_id'], place.get('attempt_number', 1), status, completed, reason)
            except TimeBudgetExpired:
                counts['deferred'] += len(places) - index
                counts['stop_reason'] = 'time_budget'
                break
            except Exception as exc:
                if isinstance(exc, GeminiDailyQuotaExhausted):
                    counts['quota_deferred'] += 1
                    counts['deferred'] += 1
                    counts['quota_stopped'] = True
                    counts['stop_reason'] = 'daily_quota_exceeded'
                    counts['deferred'] += len(places) - index - 1
                    counts['events'].append({'content_id': place['content_id'], 'status': 'quota_deferred',
                        'attempt_number': place.get('attempt_number', 1), 'reason': 'daily_quota_exceeded',
                        'committed': False, 'diagnostics': session.review_diagnostics})
                    LOG.warning('content_id=%s quota_deferred=True daily_quota_exceeded=True', place['content_id'])
                    break
                http_quota = (isinstance(exc, requests.RequestException) and exc.response is not None
                              and exc.response.status_code == 429)
                if isinstance(exc, GeminiQuotaDeferred) or http_quota:
                    if http_quota:
                        search_429_streak += 1
                    counts['quota_deferred'] += 1
                    counts['deferred'] += 1
                    counts['events'].append({'content_id':place['content_id'], 'status':'quota_deferred',
                        'attempt_number':place.get('attempt_number',1), 'reason':'provider_quota_429',
                        'committed':False, 'diagnostics':session.review_diagnostics})
                    LOG.warning('content_id=%s quota_deferred=True attempt_consumed=False',place['content_id'])
                    if isinstance(exc, GeminiCircuitOpen) or search_429_streak >= 20:
                        counts['quota_stopped'] = True
                        counts['stop_reason'] = 'consecutive_429_limit'
                        counts['deferred'] += len(places) - index - 1
                else:
                    counts['errors'] += 1
                    code = safe_error_code(exc)
                    committed = False
                    if place.get('claim_token'):
                        try:
                            committed = persist(db, place, None, [], 'error', None, code,
                                                diagnostics=session.review_diagnostics)
                        except Exception as save_exc:
                            LOG.error('content_id=%s error_state_save_failed=%s', place['content_id'], mask_sensitive(type(save_exc).__name__))
                    counts['events'].append({'content_id': place['content_id'], 'status': 'error',
                        'attempt_number': place.get('attempt_number', 1), 'reason': code,
                        'committed': committed, 'diagnostics': session.review_diagnostics})
                    LOG.error('content_id=%s pass=%s error_code=%s committed=%s',
                              place['content_id'], place.get('attempt_number', 1), code, committed)
            if isinstance(request_events, list):
                counts['events'][-1]['gemini_requests'] = request_events[event_start:]
            for field in ('quota_429_total', 'consecutive_429'):
                value = getattr(generator, field, 0)
                counts[field] = value if isinstance(value, int) else 0
            if counts['quota_stopped']:
                break
            if report_path:
                counts['elapsed_seconds'] = round(clock() - started, 1)
                write_report(report_path, counts)
            try:
                sleep(5)
            except TimeBudgetExpired:
                counts['deferred'] += len(places) - index - 1
                counts['stop_reason'] = 'time_budget'
                break
    finally:
        # Unstarted jobs don't consume an attempt. Crashes also recover after lease expiry.
        for token in {p['claim_token'] for p in places if p.get('claim_token')}:
            try:
                db.rpc('release_ai_review_work', {'claim_token': token}).execute()
            except Exception as exc:
                counts['release_errors'] += 1
                LOG.error('Review lease release failed: %s', type(exc).__name__)
    events = getattr(generator, 'request_events', [])
    if isinstance(events,list):
        counts['model_calls'] = dict(Counter(e['model'] for e in events))
        counts['model_429'] = dict(Counter(e['model'] for e in events if e.get('status')==429))
    for field in ('quota_429_total', 'consecutive_429'):
        value = getattr(generator, field, 0)
        counts[field] = value if isinstance(value,int) else 0
    counts['search_429_streak'] = search_429_streak
    counts['model_chain'] = [{'model':model,'attempts':attempts} for model,attempts in MODEL_CHAIN]
    counts['elapsed_seconds'] = round(clock() - started, 1)
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--timeout', type=int, default=1800, help='Maximum runtime in seconds (default: 1800 = 30 min)')
    parser.add_argument('--report', type=Path, default=Path('artifacts/ai-review-batch/report.json'))
    parser.add_argument('--refresh-days', type=int, default=os.getenv('AI_REVIEW_REFRESH_DAYS', '180'))
    args = parser.parse_args()
    if not 1 <= args.limit <= 50:
        parser.error('--limit must be between 1 and 50')
    if not 1 <= args.refresh_days <= 3650:
        parser.error('--refresh-days must be between 1 and 3650')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    setup_masking()
    stage = 'configuration'
    try:
        key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')
        kakao = os.getenv('KAKAO_REST_API_KEY') or os.getenv('KAKAO_API_KEY')
        if not all((os.getenv('SUPABASE_URL'), key, has_search_credentials(kakao), discover_keys())):
            raise ValueError('Missing API configuration')
        db = create_client(os.environ['SUPABASE_URL'], key,
                           options=ClientOptions(postgrest_client_timeout=15))
        stage = 'preflight'
        db.table('place_ai_summaries').select('content_id,quality_status,source_reviews').limit(1).execute()
        stage = 'refresh_queue'
        db.rpc('enqueue_due_ai_reviews', {'batch_limit': args.limit, 'refresh_days': args.refresh_days}).execute()
        deadline = time.monotonic() + args.timeout
        sleep = lambda seconds: budget_sleep(seconds, deadline)
        with BudgetSession(deadline) as session:
            generator = Gemini(session, sleep=sleep,
                               min_interval=5, max_rpm=12)
            stage = 'claim_or_process'
            counts = run(db, session, generator, kakao, args.limit, sleep=sleep, report_path=args.report)
        write_report(args.report, counts)
        message = ('[LocalTripAI] AI 리뷰 배치\n'
                   f"처리 회차: {counts['pass_number'] or '-'} / 선정: {counts['selected']}\n"
                   f"검토 시작: {counts['places_started']} / 총 처리: {counts['processed'] + counts['errors']} / 성공: {counts['success']} / 제외: {counts['excluded']}\n"
                   f"오류: {counts['errors']} / 미완료·이월: {counts['deferred']} / 소요: {counts['elapsed_seconds']}초\n"
                   f"후기 분석: {counts['review_analysis']} / 기본 안내(후기 근거 부족): {counts['basic_info']}\n"
                   f"한도 이월(시도 차감 없음): {counts['quota_deferred']} / 429 응답: {counts.get('quota_429_total',0)}회\n"
                   f"중단 사유: {counts.get('stop_reason','선정 대상 처리 완료')}\n"
                   '1차 제외·오류는 전체 1차 종료 후 1회 재시도, 2차 실패는 점검 대상으로 보존합니다.')
        LOG.info(message)
        summary_path=os.environ.get('GITHUB_STEP_SUMMARY')
        if summary_path:
            with open(summary_path,'a',encoding='utf-8') as handle:
                handle.write('```text\n'+message+'\n```\n')
        delivered = notify(message)
        LOG.info('Telegram delivered=%s', delivered)
        # Per-place failures remain queued; reaching the report ends the batch successfully.
        return 0
    except GeminiDailyQuotaExhausted as exc:
        LOG.warning('Daily quota exhausted, terminating batch gracefully: %s', mask_sensitive(str(exc)))
        return 0
    except Exception as exc:
        code = safe_error_code(exc)
        LOG.error('AI batch failed: stage=%s code=%s', stage, code)
        write_report(args.report, {'errors': 1, 'fatal': True, 'stage': stage, 'stop_reason': code})
        notify('[LocalTripAI] AI 리뷰 배치 실행 실패: ' + code)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
