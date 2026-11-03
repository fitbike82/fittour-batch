"""Apply only enqueue_due_ai_reviews from the reviewed SQL patch to batch DB.

Preserves the existing function identity, grants and unrelated review functions.
Backs up the existing definition before replacing it. No customer rows updated.
"""
import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlparse
import requests
from dotenv import dotenv_values

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    args = parser.parse_args()
    env = dotenv_values('.env')
    if urlparse(env.get('SUPABASE_URL', '')).hostname != args.project + '.supabase.co':
        raise SystemExit('Project mismatch')
    headers = {'Authorization': 'Bearer ' + env.get('SUPABASE_ACCESS_TOKEN', '')}
    def query(sql):
        r = requests.post(f'https://api.supabase.com/v1/projects/{args.project}/database/query',
                          headers=headers, json={'query': sql}, timeout=45)
        if not r.ok:
            raise RuntimeError('Management query HTTP ' + str(r.status_code))
        return r.json()
    path = Path('artifacts/ai-review-recovery')
    path.mkdir(parents=True, exist_ok=True)
    backup = query("select pg_get_functiondef('public.enqueue_due_ai_reviews(integer,integer)'::regprocedure) as definition")
    (path/'enqueue-before.sql').write_text(backup[0]['definition'], encoding='utf-8')
    source = Path('sql/ai_review_refresh.sql').read_text(encoding='utf-8')
    start = source.index('create or replace function public.enqueue_due_ai_reviews(')
    end = source.index('$$;', start) + 3
    sql = source[start:end]
    if 'pg_try_advisory_xact_lock' not in sql or 'limit batch_limit' not in sql:
        raise SystemExit('Unexpected function patch')
    query("begin; set local lock_timeout='2s'; " + sql + ' commit;')
    started = time.monotonic()
    result = query('select public.enqueue_due_ai_reviews(1,180) as enqueued')
    report = {'project': args.project, 'applied_function': 'enqueue_due_ai_reviews(integer,integer)',
              'probe_limit': 1, 'result': result, 'probe_seconds': round(time.monotonic()-started,3)}
    (path/'queue-deployment.json').write_text(json.dumps(report,indent=2), encoding='utf-8')
    print(json.dumps(report))

if __name__ == '__main__':
    main()
