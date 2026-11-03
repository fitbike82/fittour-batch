"""One-place live API dry run: reads DB, never claims/writes/notifies.

Requires explicit expected project to prevent testing an unintended database.
Uses the existing free-tier credentials; does not change billing or quotas.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from supabase import create_client, ClientOptions
from batch_ai_summary_cron import BudgetSession, search_reviews, budget_sleep
from pipeline_task2 import Gemini, MODEL, setup_masking, mask_sensitive
from gemini_key_pool import discover_keys


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--content-id', default='130712')
    parser.add_argument('--expected-project', required=True)
    parser.add_argument('--env-file', default='.env')
    parser.add_argument('--report', type=Path, default=Path('artifacts/ai-review-recovery/smoke.json'))
    parser.add_argument('--model', default=MODEL)
    args = parser.parse_args()
    load_dotenv(args.env_file)
    setup_masking()
    host = urlparse(os.getenv('SUPABASE_URL', '')).hostname
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'content_id': args.content_id,
              'database_host': host, 'db_writes': 0, 'claims': 0, 'notifications': 0, 'success': False}
    generator = None
    started = time.monotonic()
    try:
        if host != args.expected_project + '.supabase.co':
            raise ValueError('Configured DB project differs from --expected-project')
        db = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_ROLE_KEY'],
                           options=ClientOptions(postgrest_client_timeout=15))
        rows = db.table('places').select('content_id,content_type_id,title,addr1,address,detail_intro,century_store_info').eq('content_id', args.content_id).limit(1).execute().data
        if not rows:
            raise ValueError('Target place missing')
        place = rows[0]
        # Same factual context as worker, without its queue/claim maintenance.
        intro, info = place.pop('detail_intro') or {}, place.pop('century_store_info') or {}
        place['taxonomy_context'] = {k: intro[k] for k in ('firstmenu', 'treatmenu') if intro.get(k)}
        if info.get('business'):
            place['taxonomy_context']['business'] = info['business']
        address = (place.get('addr1') or place.get('address') or '').split()
        deadline = time.monotonic() + 240
        with BudgetSession(deadline) as session:
            reviews = search_reviews(session, os.getenv('KAKAO_REST_API_KEY') or os.getenv('KAKAO_API_KEY'),
                                     place['title'], sido=address[0] if address else None,
                                     sigungu=address[1] if len(address) > 1 else None)
            report['collection'] = session.review_diagnostics
            # Explicit key avoids mutating/reusing the production circuit file.
            # At most one initial generation + one existing validation repair.
            generator = Gemini(session, key=discover_keys()[0][1], model=args.model,
                               min_interval=5, max_rpm=12,
                               sleep=lambda s: budget_sleep(s, deadline))
            generator.model_chain = ((args.model, 1),)
            result, model = generator.summarize(place, reviews)
            report.update(success=True, model=model, result=result)
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        report['error'] = mask_sensitive(str(exc))[:2000]
    finally:
        report['elapsed_seconds'] = round(time.monotonic() - started, 2)
        report['gemini_requests'] = generator.request_events if generator else []
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(mask_sensitive(json.dumps(report, ensure_ascii=False, indent=2)), encoding='utf-8')
        print(mask_sensitive(json.dumps(report, ensure_ascii=False)))
    return 0 if report['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
