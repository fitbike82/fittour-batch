"""Read-only management API inspection for the explicitly selected batch DB."""
import argparse
import json
from pathlib import Path
import requests
from dotenv import dotenv_values

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--project', required=True)
args = parser.parse_args()
env = dotenv_values('.env')
response = requests.post(f'https://api.supabase.com/v1/projects/{args.project}/database/query',
    headers={'Authorization': 'Bearer '+env.get('SUPABASE_ACCESS_TOKEN', '')},
    json={'query': """select 'index' as kind, jsonb_build_object('name',indexname,'definition',indexdef) as detail
      from pg_indexes where schemaname='public' and tablename in ('places','ai_review_jobs')
      union all select 'function', jsonb_build_object('definition',pg_get_functiondef(p.oid))
      from pg_proc p join pg_namespace n on n.oid=p.pronamespace
      where n.nspname='public' and p.proname='enqueue_due_ai_reviews'
      union all select 'activity', jsonb_build_object('pid',pid,'state',state,'wait_type',wait_event_type,
          'wait_event',wait_event,'blocking_pids',pg_blocking_pids(pid))
      from pg_stat_activity where datname=current_database() and pid<>pg_backend_pid();"""}, timeout=20)
path = Path('artifacts/ai-review-recovery/db-management-check.json')
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps({'project': args.project, 'http_status': response.status_code,
    'data': response.json() if response.ok else None}), encoding='utf-8')
print('management_read_http_status='+str(response.status_code))
