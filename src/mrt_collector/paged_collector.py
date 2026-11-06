"""Bounded, resumable MyRealTrip search collector. stdlib only, local SQLite only."""
from __future__ import annotations
import argparse, datetime as dt, hashlib, json, pathlib, random, sqlite3, sys, time, uuid
from .api_client import DATA, ENDPOINTS, get_db, load_env, now, request, save_probe, product_id

DEFAULT_JOBS = [
    {"type":"tna", "city":"서울", "keyword":"입장권"},
    {"type":"tna", "city":"제주", "keyword":"입장권"},
    {"type":"tna", "city":"부산", "keyword":"요트"},
    {"type":"accommodation", "keyword":"제주 호텔", "checkin":"2026-12-10", "checkout":"2026-12-11", "adults":2},
]

def setup(con):
    con.executescript('''
    CREATE TABLE IF NOT EXISTS collection_jobs (
      job_key TEXT PRIMARY KEY, job_json TEXT NOT NULL, next_page INTEGER NOT NULL,
      status TEXT NOT NULL, last_error TEXT, total_count INTEGER, pages_done INTEGER NOT NULL DEFAULT 0,
      fetched_items INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS collection_pages (
      job_key TEXT NOT NULL REFERENCES collection_jobs(job_key), requested_page INTEGER NOT NULL,
      returned_page INTEGER, http_status INTEGER NOT NULL, item_count INTEGER NOT NULL,
      distinct_item_count INTEGER NOT NULL, total_count INTEGER, overlapping_ids INTEGER NOT NULL,
      response_file TEXT, fetched_at TEXT NOT NULL,
      PRIMARY KEY(job_key,requested_page)
    );
    CREATE TABLE IF NOT EXISTS collection_seen_ids (
      job_key TEXT NOT NULL REFERENCES collection_jobs(job_key), product_id TEXT NOT NULL,
      first_page INTEGER NOT NULL, PRIMARY KEY(job_key, product_id)
    );
    ''' )
    con.execute('''CREATE TABLE IF NOT EXISTS collection_overlap_events (
      job_key TEXT NOT NULL, requested_page INTEGER NOT NULL, product_id TEXT NOT NULL, first_page INTEGER NOT NULL,
      detected_at TEXT NOT NULL, PRIMARY KEY(job_key,requested_page,product_id))''')
    con.commit()

def canonical_job(j):
    kind=j.get('type')
    if kind not in ('tna','accommodation'): raise ValueError('type은 tna 또는 accommodation')
    if not isinstance(j.get('keyword'),str) or not j['keyword'].strip(): raise ValueError('keyword 필수')
    base={'type':kind,'keyword':j['keyword'].strip()}
    if kind=='tna':
        if not isinstance(j.get('city'),str) or not j['city'].strip():raise ValueError('tna는 city 필수')
        base['city']=j['city'].strip()
    else:
        for key in ('checkin','checkout'):
            dt.date.fromisoformat(j[key]);base[key]=j[key]
        if base['checkout']<=base['checkin']:raise ValueError('checkout은 checkin 이후')
        adults=int(j.get('adults',2))
        if not 1<=adults<=10:raise ValueError('adults 1~10')
        base['adults']=adults
    return base

def job_key(job):return hashlib.sha256(json.dumps(job,ensure_ascii=False,sort_keys=True).encode()).hexdigest()[:24]
def initial_page(job):return 1 if job['type']=='tna' else 0

def make_payload(job,page):
    if job['type']=='tna':return {'keyword':job['keyword'],'city':job['city'],'page':page}
    return {'keyword':job['keyword'],'checkIn':job['checkin'],'checkOut':job['checkout'],'adultCount':job['adults'],'page':page}

def info_from_response(resp):
    body=resp.get('body');data=body.get('data') if isinstance(body,dict) else None
    if not isinstance(data,dict) or not isinstance(data.get('items'),list):raise ValueError('data.items 배열이 없음')
    items=data['items']
    if any(not isinstance(row,dict) for row in items):raise ValueError('items에 객체 아닌 값 존재')
    return data,items

def pause_until_retry(resp,retry_idx,delay):
    h={k.lower():v for k,v in resp.get('headers',{}).items()}
    retry=h.get('retry-after')
    try: wait=min(120,max(delay,float(retry))) if retry is not None else min(120,max(delay,2**retry_idx*delay))
    except (ValueError,TypeError): wait=min(120,max(delay,2**retry_idx*delay))
    return wait

def call_bounded(path,payload,max_retries,delay):
    for attempt in range(max_retries+1):
        resp=request(path,payload)
        if resp['http_status'] not in (429,500,502,503,504):return resp
        if attempt>=max_retries:return resp
        wait=pause_until_retry(resp,attempt,delay)
        print(f'  HTTP {resp["http_status"]}, {wait:g}초 후 재시도 ({attempt+1}/{max_retries})')
        time.sleep(wait)
    raise AssertionError('unreachable')

def ensure_job(con,job,resume):
    key=job_key(job)
    stored=con.execute('SELECT next_page,status FROM collection_jobs WHERE job_key=?',(key,)).fetchone()
    if stored and not resume:raise ValueError(f'기존 수집 작업 발견: {key}. --resume로 재개하세요')
    if not stored:
        con.execute('INSERT INTO collection_jobs(job_key,job_json,next_page,status,updated_at) VALUES(?,?,?,?,?)',
                    (key,json.dumps(job,ensure_ascii=False),initial_page(job),'READY',now()));con.commit()
        return key,initial_page(job),'READY'
    return key,*stored

def mark_state(con,key,status,error=None):
    con.execute('UPDATE collection_jobs SET status=?,last_error=?,updated_at=? WHERE job_key=?',(status,error,now(),key));con.commit()

def persist_diagnostic(job_key_value, page, response, reason, overlaps=None):
    """Write redacted-ish response metadata and product IDs, no API credentials."""
    directory=DATA/'diagnostics'/'v43';directory.mkdir(parents=True,exist_ok=True)
    body=response.get('body') if isinstance(response,dict) else None
    data=body.get('data') if isinstance(body,dict) else None
    items=data.get('items',[]) if isinstance(data,dict) else []
    report={
        'job_key':job_key_value,'requested_page':page,'at':now(),'reason':reason,
        'returned_page':data.get('page') if isinstance(data,dict) else None,
        'total_count':data.get('totalCount') if isinstance(data,dict) else None,
        'has_next_page':data.get('hasNextPage') if isinstance(data,dict) else None,
        'item_ids':[product_id(x) for x in items if isinstance(x,dict)],
        'overlaps':overlaps or [],
    }
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    path=directory/f'{job_key_value}_page_{page}_{stamp}_{uuid.uuid4().hex[:8]}.json'
    with path.open('x',encoding='utf-8') as f:
        f.write(json.dumps(report,ensure_ascii=False,indent=2))
    return path


def persist_page_index(job_key_value, page, data, ids, response):
    directory=DATA/'diagnostics'/'v43'/'pages';directory.mkdir(parents=True,exist_ok=True)
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    path=directory/f'{job_key_value}_page_{page}_{stamp}_{uuid.uuid4().hex[:8]}.json'
    record={'job_key':job_key_value,'requested_page':page,'returned_page':data.get('page'),
      'total_count':data.get('totalCount'),'has_next_page':data.get('hasNextPage'),
      'item_ids':ids,'response_at':response.get('at')}
    with path.open('x',encoding='utf-8') as f:json.dump(record,f,ensure_ascii=False,indent=2)
    return path

def evaluate_overlap(items, seen_map, overlap_policy, max_overlap_ratio):
    ids=[product_id(x) for x in items]
    duplicates=len(ids)-len(set(ids))
    overlaps=[{'product_id':pid,'first_page':seen_map[pid]} for pid in sorted(set(ids)) if pid in seen_map]
    distinct_new=len(set(ids))-len(overlaps)
    if duplicates: return ids,overlaps,distinct_new,'페이지 내부 중복 ID'
    if overlaps and overlap_policy=='stop':return ids,overlaps,distinct_new,'이전 페이지와 ID 중복'
    if overlaps and (not distinct_new or len(overlaps)/len(ids)>max_overlap_ratio):
        return ids,overlaps,distinct_new,'중복률 초과 또는 신규 상품 0건'
    return ids,overlaps,distinct_new,None

def collect(args):
    load_env();con=get_db();setup(con)
    raw=json.loads(pathlib.Path(args.jobs).read_text(encoding='utf-8-sig'))
    jobs=raw['jobs'] if isinstance(raw,dict) else raw
    if not isinstance(jobs,list) or not jobs:raise ValueError('jobs는 1건 이상의 배열이어야 합니다')
    jobs=[canonical_job(x) for x in jobs]
    calls=0;access_denied=False
    for job in jobs:
        key,page,state=ensure_job(con,job,args.resume)
        if state in ('DONE','DONE_WITH_GAPS','NEEDS_REVIEW'):print(f'SKIP {state} {key}: {job}');continue
        if state in ('TRUNCATED','ERROR') and args.resume:print(f'RESUMING {state} {key}')
        print(f'JOB {key}: {job} / 시작 page={page}')
        mark_state(con,key,'RUNNING')
        page_count=0
        while page_count<args.max_pages_per_job and calls<args.max_calls:
            payload=make_payload(job,page)
            resp=None
            diagnostic_written=False
            try:
                resp=call_bounded(ENDPOINTS[job['type']],payload,args.max_retries,args.delay)
                calls+=1
                if resp['http_status']!=200:raise RuntimeError(f'HTTP {resp["http_status"]}: {str(resp.get("body"))[:160]}')
                data,items=info_from_response(resp)
                if data.get('page')!=page:raise ValueError(f'page 불일치: 요청={page}, 반환={data.get("page")}')
                ids=[product_id(x) for x in items]
                seen_map={r[0]:r[1] for r in con.execute('SELECT product_id,first_page FROM collection_seen_ids WHERE job_key=?',(key,))}
                ids,overlaps,new_count,issue=evaluate_overlap(items,seen_map,args.overlap_policy,args.max_overlap_ratio)
                if overlaps or issue:
                    diag=persist_diagnostic(key,page,resp,issue or '중복 기록 후 진행',overlaps)
                    diagnostic_written=True
                    print(f'  진단 저장: {diag} / overlap={len(overlaps)}, new={new_count}')
                if issue:raise ValueError(f'{issue}: overlap={len(overlaps)} new={new_count}')
                total=data.get('totalCount');has_next=data.get('hasNextPage')
                if not items and (has_next is True):raise ValueError('빈 페이지인데 hasNextPage=true')
                saved=save_probe(con,job['type'],job['keyword'],job.get('city'),resp)
                # save_probe retains the full redacted response; this lightweight index makes page comparison easier.
                persist_page_index(key,page,data,ids,resp)
                # A checkpoint only moves forward after response persisted and product inventory is upserted.
                with con:
                    for pid in set(ids):con.execute('INSERT OR IGNORE INTO collection_seen_ids VALUES(?,?,?)',(key,pid,page))
                    for overlap in overlaps:con.execute('INSERT OR IGNORE INTO collection_overlap_events VALUES(?,?,?,?,?)',(key,page,overlap['product_id'],overlap['first_page'],now()))
                    con.execute('''INSERT INTO collection_pages(job_key,requested_page,returned_page,http_status,item_count,distinct_item_count,total_count,overlapping_ids,response_file,fetched_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)''',(key,page,data.get('page'),200,len(items),new_count,total,len(overlaps),saved['response_path'],now()))
                    con.execute('''UPDATE collection_jobs SET next_page=?,pages_done=pages_done+1,
                      fetched_items=fetched_items+?,total_count=?,last_error=NULL,updated_at=? WHERE job_key=?''',
                      (page+1,len(items),total,now(),key))
                page_count+=1
                print(f'  page={page} rows={len(items)} 신규ID={new_count} 중복ID={len(overlaps)} total={total} hasNext={has_next}; 저장 완료')
                cumulative=con.execute('SELECT COUNT(*) FROM collection_seen_ids WHERE job_key=?',(key,)).fetchone()[0]
                is_end=(has_next is False or not items or (isinstance(total,int) and total>=0 and cumulative>=total))
                if is_end:
                    incomplete=(isinstance(total,int) and cumulative<total)
                    state='DONE_WITH_GAPS' if incomplete else 'DONE'
                    mark_state(con,key,state, f'API 종료 후 고유 {cumulative}/{total}; 전체 수집 보장 불가' if incomplete else None)
                    print(f'  종료 {state}: 고유ID={cumulative}, totalCount={total} (검색 결과 완전성 별도 검증)')
                    break
                page+=1
                if page_count<args.max_pages_per_job and calls<args.max_calls:time.sleep(args.delay)
            except Exception as ex:
                if resp is not None and not diagnostic_written:
                    persist_diagnostic(key,page,resp,f'{type(ex).__name__}: {str(ex)[:200]}')
                code=resp.get('http_status') if isinstance(resp,dict) else None
                mark_state(con,key,'RETRY' if code in (429,500,502,503,504) or isinstance(ex,(TimeoutError,ConnectionError)) else 'ERROR',f'{type(ex).__name__}: {str(ex)[:300]}')
                access_denied=code in (401,403)
                print(f'  중단: {type(ex).__name__}: {ex}',file=sys.stderr)
                break
        else:
            if con.execute('SELECT status FROM collection_jobs WHERE job_key=?',(key,)).fetchone()[0]=='RUNNING':
                mark_state(con,key,'TRUNCATED','안전 상한 도달. --resume로 재개')
        if access_denied:print('인증/접근 거부: 전체 검색 실행 중단');break
        if calls>=args.max_calls:print('전체 호출 상한 도달. 다음 실행에서 --resume');break
        time.sleep(args.delay)
    report(con)

def report(con):
    records=[dict(zip(['job_key','job','next_page','status','pages_done','fetched_items','total_count','last_error'],r)) for r in con.execute('SELECT job_key,job_json,next_page,status,pages_done,fetched_items,total_count,last_error FROM collection_jobs ORDER BY updated_at DESC')]
    DATA.mkdir(exist_ok=True);(DATA/'collection_v4_report.json').write_text(json.dumps({'generated_at':now(),'jobs':records},ensure_ascii=False,indent=2),encoding='utf-8')
    print('결과: data/collection_v4_report.json; 상품 DB: data/inventory.sqlite3')

def main():
    p=argparse.ArgumentParser(description='MRT V4 안전한 페이지 순회 및 중단 재개')
    p.add_argument('--jobs',default=str(DATA.parent/'config'/'collection_jobs.json'))
    p.add_argument('--max-pages-per-job',type=int,default=3)
    p.add_argument('--max-calls',type=int,default=12)
    p.add_argument('--max-retries',type=int,default=2)
    p.add_argument('--delay',type=float,default=2.0)
    p.add_argument('--resume',action='store_true',help='동일 조건의 다음 미완료 페이지부터 재개')
    p.add_argument('--overlap-policy',choices=['stop','record'],default='stop',help='중복 페이지를 기록만 하고 진행하려면 record (기본은 안전 중단)')
    p.add_argument('--max-overlap-ratio',type=float,default=0.5,help='record 모드 허용 최대 중복 비율, 기본 0.5')
    a=p.parse_args()
    if not 1<=a.max_pages_per_job<=250:p.error('--max-pages-per-job 1~250')
    if not 1<=a.max_calls<=500:p.error('--max-calls 1~500')
    if not 0<=a.max_retries<=4:p.error('--max-retries 0~4')
    if a.delay<1:p.error('--delay 최소 1초')
    if not 0<=a.max_overlap_ratio<=1:p.error('--max-overlap-ratio 0~1')
    try:collect(a)
    except (ValueError,KeyError,sqlite3.Error,OSError,json.JSONDecodeError) as e:
        print(f'중단: {e}',file=sys.stderr);sys.exit(2)
if __name__=='__main__':main()
