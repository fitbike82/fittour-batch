"""Two-pass review queue: python batch_ai_summary_cron.py --limit 50."""
import argparse
from collections import Counter
import json
import logging
import os
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from supabase import create_client, ClientOptions

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
    elif not (host.endswith('.tistory.com') or host == 'brunch.co.kr'):
        raise ValueError('Unsupported article host')
    response = session.get(url, timeout=15, allow_redirects=False)
    response.raise_for_status()
    soup = BeautifulSoup(response.content, 'html.parser')
    for element in soup.select('script,style,noscript,nav,header,footer'):
        element.decompose()
    container = soup.select_one('.se-main-container, #postViewArea, .tt_article_useless_p_margin, #article-view, .entry-content, .wrap_body')
    if container is None:
        raise ValueError('Unreadable article')
    paragraphs = [p.strip() for p in container.get_text('\n', strip=True).splitlines() if p.strip()]
    # A disclosed sponsored post is dropped entirely: removing the disclosure alone
    # does not turn the remaining review into an independent opinion.
    if any(sponsored(p) for p in paragraphs):
        raise ValueError('Sponsored article')
    text = ' '.join(paragraphs)
    if len(text) < 150:
        raise ValueError('Article too short')
    return review_excerpt(text)


def search_reviews(session, key, title):
    reviews, seen = [], set()
    diagnostics = {'queries': 0, 'documents': 0, 'article_errors': {}, 'filtered': {}}
    session.review_diagnostics = diagnostics
    unavailable = 0
    for suffix in ('내돈내산', '솔직 후기', '방문 후기'):
        response = session.get('https://dapi.kakao.com/v2/search/blog',
            headers={'Authorization': f'KakaoAK {key}'},
            params={'query': f'{title} {suffix}', 'size': 10, 'sort': 'accuracy'}, timeout=15)
        response.raise_for_status()
        diagnostics['queries'] += 1
        for doc in response.json().get('documents', []):
            diagnostics['documents'] += 1
            url = doc.get('url', '')
            if not url or url in seen:
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
                            'url': url, 'query': f'{title} {suffix}', 'text': text})
            if len(reviews) == 5:
                break
        if len(reviews) >= 2:
            break
    diagnostics['readable'] = len(reviews)
    if len(reviews) < 2 and unavailable:
        raise ReviewCollectionUnavailable('Insufficient sources after temporary article failures')
    return reviews


def pending_places(db, limit):
    return db.rpc('claim_ai_review_work', {'batch_limit': limit}).execute().data or []


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
            'reviews_data': [{k: r[k] for k in ('source_id', 'title', 'url', 'query')} for r in reviews],
            'diagnostics_data': diagnostics or {},
        }).execute().data)
    payload = {'content_id': place['content_id'], 'quality_status': status, 'model': model,
        'honest_summary': '\n'.join(summary['summary_lines']) if summary else None,
        'pros': summary['pros'] if summary else [], 'cons': summary['cons'] if summary else [],
        'visit_tips': summary['visit_tips'] if summary else None,
        'source_reviews': {'input_modified_time': place.get('modified_time'), 'reason': reason,
            'reviews': [{k: r[k] for k in ('source_id', 'title', 'url', 'query')} for r in reviews]},
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
    if isinstance(exc, requests.RequestException):
        return f'HTTP_{exc.response.status_code}' if exc.response is not None else mask_sensitive(type(exc).__name__)
    if isinstance(exc, RuntimeError) and str(exc).startswith('Gemini request failed ('):
        return mask_sensitive(str(exc))  # Gemini emits a controlled class/status message, never request URLs.
    return mask_sensitive(type(exc).__name__)


def write_report(path, counts):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'checked_at': datetime.now(timezone.utc).isoformat(), **counts},
                               ensure_ascii=False, indent=2), encoding='utf-8')


def run(db, session, generator, kakao_key, limit, sleep=time.sleep, clock=time.monotonic, report_path=None):
    started = clock()
    counts = {'processed': 0, 'success': 0, 'excluded': 0, 'errors': 0, 'deferred': 0, 'quota_deferred': 0, 'quota_stopped': False, 'places_started': 0}
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
                    reviews = search_reviews(session, kakao_key, place['title'])
                    search_429_streak = 0
                    try:
                        summary, model = generator.summarize(place, reviews)
                        status = 'ready'
                    except ValueError as exc:
                        reason = str(exc)
                        if not reason.startswith(EXCLUDED_REASONS):
                            raise
                        actual_model = getattr(generator, 'last_model', None)
                        summary, model, status = None, actual_model if isinstance(actual_model,str) else MODEL, 'insufficient'
                    completed = persist(db, place, summary, reviews, status, model, reason,
                                        diagnostics=session.review_diagnostics)
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
    args = parser.parse_args()
    if not 1 <= args.limit <= 50:
        parser.error('--limit must be between 1 and 50')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    setup_masking()
    try:
        key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')
        kakao = os.getenv('KAKAO_REST_API_KEY') or os.getenv('KAKAO_API_KEY')
        if not all((os.getenv('SUPABASE_URL'), key, kakao, discover_keys())):
            raise ValueError('Missing API configuration')
        db = create_client(os.environ['SUPABASE_URL'], key,
                           options=ClientOptions(postgrest_client_timeout=15))
        db.table('place_ai_summaries').select('content_id,quality_status,source_reviews').limit(1).execute()
        deadline = time.monotonic() + args.timeout
        sleep = lambda seconds: budget_sleep(seconds, deadline)
        with BudgetSession(deadline) as session:
            generator = Gemini(session, sleep=sleep,
                               min_interval=5, max_rpm=12)
            counts = run(db, session, generator, kakao, args.limit, sleep=sleep, report_path=args.report)
        write_report(args.report, counts)
        message = ('[LocalTripAI] AI 리뷰 배치\n'
                   f"처리 회차: {counts['pass_number'] or '-'} / 선정: {counts['selected']}\n"
                   f"검토 시작: {counts['places_started']} / 총 처리: {counts['processed'] + counts['errors']} / 성공: {counts['success']} / 제외: {counts['excluded']}\n"
                   f"오류: {counts['errors']} / 미완료·이월: {counts['deferred']} / 소요: {counts['elapsed_seconds']}초\n"
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
        LOG.error('AI batch failed: %s', mask_sensitive(type(exc).__name__))
        notify('[LocalTripAI] AI 리뷰 배치 실행 실패: ' + mask_sensitive(type(exc).__name__))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
