"""V4.4 accommodation discovery: bounded rounds, immutable observation history.

Does not use the page-based V4 collection_jobs checkpoint. Does not claim exhaustive coverage.
Only performs documented accommodation search POST; local SQLite and JSON records.
"""
from __future__ import annotations
import argparse, csv, datetime as dt, hashlib, json, pathlib, sqlite3, sys, time, uuid
from collections import Counter
from .api_client import DATA, ENDPOINTS, get_db, load_env, now, product_id, save_probe
from .paged_collector import call_bounded, info_from_response

DEFAULT = DATA.parent / 'config' / 'accommodation_jobs_v44.json'

def canonical(j):
    if not isinstance(j,dict) or not str(j.get('keyword','')).strip(): raise ValueError('keyword 필수')
    start=dt.date.fromisoformat(j['checkin']);end=dt.date.fromisoformat(j['checkout'])
    if end<=start: raise ValueError('checkout은 checkin 이후')
    adults=int(j.get('adults',2))
    if not 1<=adults<=10: raise ValueError('adults 1~10')
    return {'keyword':j['keyword'].strip(),'checkin':start.isoformat(),'checkout':end.isoformat(),'adults':adults}

def key_for(job):
    return hashlib.sha256(json.dumps(job,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()[:24]

def payload_for(job,page):
    return {'keyword':job['keyword'],'checkIn':job['checkin'],'checkOut':job['checkout'],'adultCount':job['adults'],'page':page}

def evaluate(previous, ids, global_seen):
    if len(ids)!=len(set(ids)): raise ValueError('페이지 내부 동일 ID 중복: 원본 확인 필요')
    s=set(ids)
    return {'unique_in_page':len(s),'new_in_job':len(s-global_seen),
            'overlap_in_job':len(s&global_seen),
            'same_page_previous_common':len(s & previous) if previous is not None else None,
            'same_page_previous_jaccard':round(len(s&previous)/len(s|previous),4) if previous is not None and s|previous else None,
            'same_page_previous_same_order':None}

def setup(con):
    con.executescript('''
    CREATE TABLE IF NOT EXISTS stay44_sessions (
      session_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
      status TEXT NOT NULL, jobs_count INTEGER NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
      note TEXT NOT NULL DEFAULT 'OBSERVED_ONLY_NOT_EXHAUSTIVE');
    CREATE TABLE IF NOT EXISTS stay44_observations (
      session_id TEXT NOT NULL, job_key TEXT NOT NULL, keyword TEXT NOT NULL,
      checkin TEXT NOT NULL, checkout TEXT NOT NULL, adults INTEGER NOT NULL,
      round_number INTEGER NOT NULL, requested_page INTEGER NOT NULL,
      requested_at TEXT NOT NULL, status_code INTEGER NOT NULL,
      reported_total INTEGER, returned_page INTEGER,
      ids_json TEXT NOT NULL, raw_response_path TEXT,
      count INTEGER NOT NULL, new_in_job INTEGER NOT NULL,
      overlap_in_job INTEGER NOT NULL, same_page_jaccard REAL,
      error TEXT, PRIMARY KEY(session_id,job_key,round_number,requested_page));
    CREATE INDEX IF NOT EXISTS stay44_obs_job ON stay44_observations(job_key,requested_page,requested_at);
    ''')
    con.commit()

def known_ids(con, key):
    out=set()
    for (raw,) in con.execute('SELECT ids_json FROM stay44_observations WHERE job_key=? AND status_code=200',(key,)):
        out.update(json.loads(raw))
    return out

def last_page_ids(con,key,page):
    row=con.execute('SELECT ids_json FROM stay44_observations WHERE job_key=? AND requested_page=? AND status_code=200 ORDER BY requested_at DESC LIMIT 1',(key,page)).fetchone()
    return set(json.loads(row[0])) if row else None

def safe_raw(con, resp, job):
    # save_probe stores the API response and performs the existing normalized product UPSERT.
    return save_probe(con,'accommodation',job['keyword'],None,resp)['response_path']

def run(args):
    load_env()
    jobs_raw=json.loads(pathlib.Path(args.jobs).read_text(encoding='utf-8-sig'))
    jobs_raw=jobs_raw['jobs'] if isinstance(jobs_raw,dict) else jobs_raw
    if not isinstance(jobs_raw,list) or not jobs_raw: raise ValueError('작업 배열 없음')
    jobs=[canonical(j) for j in jobs_raw]
    con=get_db();setup(con)
    session=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'_'+uuid.uuid4().hex[:8]
    con.execute('INSERT INTO stay44_sessions(session_id,started_at,status,jobs_count) VALUES(?,?,?,?)',(session,now(),'RUNNING',len(jobs)));con.commit()
    used=0;had_errors=False
    try:
        for job in jobs:
            key=key_for(job);seen=known_ids(con,key)
            print(f'JOB {key}: {job["keyword"]}, 이전 세션 고유={len(seen)}')
            for round_no in range(1,args.rounds+1):
                if used>=args.max_calls: break
                round_new=0
                for page in range(args.start_page,args.start_page+args.max_pages):
                    if used>=args.max_calls:break
                    response=None;error=None;items=[];ids=[];reported=None;returned=None;path=None
                    try:
                        response=call_bounded(ENDPOINTS['accommodation'],payload_for(job,page),args.max_retries,args.delay)
                        used+=1
                        if response['http_status']!=200: raise RuntimeError('HTTP '+str(response['http_status']))
                        data,items=info_from_response(response)
                        returned=data.get('page');reported=data.get('totalCount')
                        if returned != page: raise ValueError('요청 페이지/응답 페이지 불일치')
                        ids=[product_id(x) for x in items]
                        if len(ids)!=len(set(ids)):raise ValueError('페이지 내부 동일 ID 중복')
                        old_page=last_page_ids(con,key,page)
                        metric=evaluate(old_page,ids,seen)
                        path=safe_raw(con,response,job)
                        new=metric['new_in_job'];overlap=metric['overlap_in_job'];round_new+=new
                        seen.update(ids)
                        jac=metric['same_page_previous_jaccard']
                        status='PASS'
                    except Exception as exc:
                        had_errors=True;error=f'{type(exc).__name__}: {str(exc)[:200]}'
                        new=0;overlap=0;jac=None;status='ERROR'
                        # Retain failed response for forensic use without overwriting previous diagnostics.
                        if response is not None:
                            dirpath=DATA/'diagnostics'/'v44';dirpath.mkdir(parents=True,exist_ok=True)
                            p=dirpath/(session+'_'+key+f'_round{round_no}_page{page}.json')
                            p.write_text(json.dumps(response,ensure_ascii=False,indent=2),encoding='utf-8');path=str(p.relative_to(DATA.parent))
                    http=response['http_status'] if response else 0
                    con.execute('''INSERT INTO stay44_observations
                      (session_id,job_key,keyword,checkin,checkout,adults,round_number,requested_page,requested_at,status_code,reported_total,returned_page,ids_json,raw_response_path,count,new_in_job,overlap_in_job,same_page_jaccard,error)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                      (session,key,job['keyword'],job['checkin'],job['checkout'],job['adults'],round_no,page,now(),http,reported,returned,json.dumps(ids,ensure_ascii=False),path,len(ids),new,overlap,jac,error))
                    con.commit()
                    print(f'  round={round_no} page={page} HTTP={http} rows={len(ids)} 신규={new} 중복={overlap} Jaccard={jac} {status}')
                    if error: print('  중단/오류: '+error,file=sys.stderr);break
                    if not items:break
                    if used<args.max_calls:time.sleep(args.delay)
                if used>=args.max_calls or had_errors:break
                print(f'  round={round_no} 신규={round_new} (완전 수집 보장하지 않음)')
            if used>=args.max_calls or had_errors:break
    finally:
        state='ERROR' if had_errors else 'BOUNDED'
        con.execute('UPDATE stay44_sessions SET ended_at=?, status=?,calls=? WHERE session_id=?',(now(),state,used,session));con.commit()
        make_report(con,session)
        con.close()
    print(f'SESSION {session} calls={used} status={state}; 모든 수집 결과는 관측 기록이며 전체 확보를 의미하지 않습니다')

def make_report(con,session):
    rows=[]
    for row in con.execute('''SELECT job_key,keyword,COUNT(*),SUM(count),SUM(new_in_job),SUM(overlap_in_job),MIN(same_page_jaccard),MAX(reported_total)
                            FROM stay44_observations WHERE session_id=? GROUP BY job_key,keyword ORDER BY keyword''',(session,)):
        rows.append(dict(zip(('job_key','keyword','pages_observed','returned_rows','first_time_ids','overlap_rows','min_same_page_jaccard','api_reported_total'),row)))
    output={'session_id':session,'interpretation':'OBSERVED_ONLY_NOT_EXHAUSTIVE','jobs':rows}
    folder=DATA/'analysis_v44';folder.mkdir(parents=True,exist_ok=True)
    (folder/'latest_session.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    with (folder/'latest_session.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]) if rows else ['job_key','keyword']);w.writeheader();w.writerows(rows)
    print('보고서: data/analysis_v44/latest_session.json')

def main():
    p=argparse.ArgumentParser(description='숙소 검색 변동 추적/고유 상품 누적 (V4.4, 전체 수집 보장 없음)')
    p.add_argument('--jobs',default=str(DEFAULT));p.add_argument('--rounds',type=int,default=2)
    p.add_argument('--start-page',type=int,default=0);p.add_argument('--max-pages',type=int,default=2)
    p.add_argument('--max-calls',type=int,default=8);p.add_argument('--delay',type=float,default=2.0)
    p.add_argument('--max-retries',type=int,default=1)
    args=p.parse_args()
    if not 1<=args.rounds<=5 or not 1<=args.max_pages<=10 or not 1<=args.max_calls<=40 or args.start_page<0 or args.delay<1 or not 0<=args.max_retries<=2:p.error('안전 실행 상한 위반')
    try:run(args)
    except (ValueError,KeyError,sqlite3.Error,OSError,json.JSONDecodeError) as e:
        print('실행 실패: '+str(e),file=sys.stderr);sys.exit(2)
if __name__=='__main__': main()
