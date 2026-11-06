"""Nationwide MyRealTrip *partner API* search scheduler (stdlib-only).

Keeps geographic search hints separate from verified property locations. No AI matching.
Execute within the existing affiliate-analyzer project, alongside mrt_analyzer.py.
"""
from __future__ import annotations
import argparse, csv, datetime as dt, hashlib, json, pathlib, sqlite3, sys, time, urllib.request
from .api_client import DATA, ENDPOINTS, get_db, load_env, now, request, product_id, save_probe

REGION_URL = 'https://raw.githubusercontent.com/cosmosfarm/korea-administrative-district/master/korea-administrative-district.json'
# Verified source is province/district only; town-level coverage is NOT available.
EXPECTED_PROVINCES = frozenset(('서울특별시','부산광역시','대구광역시','인천광역시','광주광역시','대전광역시','울산광역시','세종특별자치시','경기도','강원특별자치도','충청북도','충청남도','전북특별자치도','전라남도','경상북도','경상남도','제주특별자치도'))
REGION_PATH = DATA / 'regions_kr.json'
DEFAULT_TNA_TERMS = ('입장권','체험','투어','액티비티','공연','전시','레저')
DEFAULT_STAY_TERMS = ('호텔','리조트','펜션','모텔','게스트하우스','숙소')


def normalize_regions(content):
    """Parse the verified cosmosfarm 17-province/municipality dataset.

    The source does not contain eup/myeon/dong. Never fabricate level 3.
    """
    if not isinstance(content, dict) or not isinstance(content.get('data'), list):
        raise ValueError('지원하지 않는 행정구역 원본 형식: expected object.data array')
    rows=[]
    for entry in content['data']:
        if not isinstance(entry, dict) or len(entry)!=1:
            raise ValueError('잘못된 시도 항목 구조')
        province,districts=next(iter(entry.items()))
        if not isinstance(province,str) or not isinstance(districts,list):
            raise ValueError('시도/시군구 자료형 오류')
        if not all(isinstance(d,str) and d.strip() for d in districts):
            raise ValueError('시군구 이름 오류')
        rows.append({'depth':1,'sido':province,'sigungu':'','dong':'','query':province,'source_code':''})
        if len(districts)!=len(set(districts)):
            raise ValueError(f'중복 시군구 감지: {province}')
        for district in districts:
            rows.append({'depth':2,'sido':province,'sigungu':district,'dong':'',
                         'query':f'{province} {district}','source_code':''})
    return rows


def validate_regions(regions):
    provinces=[r['sido'] for r in regions if r['depth']==1]
    missing=sorted(EXPECTED_PROVINCES-set(provinces))
    extra=sorted(set(provinces)-EXPECTED_PROVINCES)
    duplicate=sorted({x for x in provinces if provinces.count(x)>1})
    if missing or extra or duplicate or len(provinces)!=17:
        raise ValueError(f'시도 검증 실패: 누락={missing}, 중복={duplicate}, 예외={extra}; 총 {len(provinces)}개')
    if not any(r['depth']==2 for r in regions):
        raise ValueError('시군구가 0건입니다')
    return regions


def fetch_regions(url=REGION_URL):
    with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'FitTour-MRT-Discovery/1.1'}),timeout=30) as r:
        raw=r.read(2_000_000)
    obj=json.loads(raw.decode('utf-8-sig'))
    regions=validate_regions(normalize_regions(obj))
    REGION_PATH.parent.mkdir(parents=True,exist_ok=True)
    output={'source':url,'source_version':obj.get('version'), 'retrieved_at':now(),
            'coverage_depths':[1,2],'depth3_available':False, 'regions':regions}
    # Atomic replace: never corrupt a valid region list on interrupted download.
    temp=REGION_PATH.with_suffix('.json.tmp')
    temp.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    temp.replace(REGION_PATH)
    return regions


def load_regions():
    if not REGION_PATH.exists():
        raise FileNotFoundError('지역 목록이 없습니다. init-regions 명령을 먼저 실행하세요')
    obj=json.loads(REGION_PATH.read_text(encoding='utf-8'))
    rows=validate_regions(obj['regions'])
    if obj.get('source')!=REGION_URL or obj.get('depth3_available') is not False:
        raise ValueError('지역 파일의 원천/범위가 현재 검증된 데이터와 다릅니다. init-regions를 다시 실행하세요')
    return rows


def setup(con):
    con.executescript('''
    CREATE TABLE IF NOT EXISTS nationwide_jobs(
      job_id TEXT PRIMARY KEY, phase TEXT NOT NULL, region_json TEXT NOT NULL,
      keyword TEXT NOT NULL, checkin TEXT, checkout TEXT, adults INTEGER,
      next_page INTEGER NOT NULL, status TEXT NOT NULL, error TEXT,
      pages INTEGER NOT NULL DEFAULT 0, returned INTEGER NOT NULL DEFAULT 0,
      unique_seen INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS nationwide_pages(
      job_id TEXT NOT NULL, page INTEGER NOT NULL, response_path TEXT,
      http_status INTEGER NOT NULL, returned INTEGER NOT NULL,
      new_ids INTEGER NOT NULL, overlap_ids INTEGER NOT NULL,
      reported_total INTEGER, has_next TEXT, fetched_at TEXT NOT NULL,
      PRIMARY KEY(job_id,page));
    CREATE TABLE IF NOT EXISTS nationwide_seen(
      job_id TEXT NOT NULL, provider_id TEXT NOT NULL,
      PRIMARY KEY(job_id,provider_id));
    CREATE TABLE IF NOT EXISTS nationwide_product_observations(
      phase TEXT NOT NULL, provider_id TEXT NOT NULL, job_id TEXT NOT NULL,
      source_region_json TEXT NOT NULL, query_keyword TEXT NOT NULL,
      observed_at TEXT NOT NULL,
      PRIMARY KEY(phase,provider_id,job_id));
    ''')
    con.commit()


def make_jobs(regions,phase,depths,terms,checkin=None,checkout=None,adults=2):
    for r in regions:
        if r['depth'] not in depths:continue
        for term in terms:
            kw=(r['query']+' '+term)
            spec={'phase':phase,'region':r,'keyword':kw,'checkin':checkin if phase=='stays' else None,
                  'checkout':checkout if phase=='stays' else None,'adults':adults if phase=='stays' else None}
            spec['job_id']=hashlib.sha256(json.dumps(spec,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:24]
            yield spec


def plan(con,phase,depths,terms,checkin,checkout,adults):
    if phase=='stays':
        if not checkin or not checkout:raise ValueError('숙소는 체크인/아웃 날짜가 필요합니다')
        if dt.date.fromisoformat(checkout)<=dt.date.fromisoformat(checkin):raise ValueError('체크아웃 날짜 오류')
    regions=load_regions()
    available_depths={r['depth'] for r in regions}
    if not depths.issubset(available_depths):
        raise ValueError(f'해당 지역 원천은 depths={sorted(available_depths)}만 지원합니다. 읍면동 목록은 제공되지 않으며 임의 생성하지 않습니다')
    jobs=list(make_jobs(regions,phase,depths,terms,checkin,checkout,adults))
    for j in jobs:
        con.execute('''INSERT OR IGNORE INTO nationwide_jobs(job_id,phase,region_json,keyword,checkin,checkout,adults,next_page,status,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',(j['job_id'],j['phase'],json.dumps(j['region'],ensure_ascii=False),j['keyword'],j['checkin'],j['checkout'],j['adults'],1 if phase=='tna' else 0,'PENDING',now()))
    con.commit()
    print(f'생성/확인 작업 {len(jobs)}건, phase={phase}, depths={sorted(depths)}. 기존 작업 상태 유지')


def payload(phase,reg,kw,page,checkin,checkout,adults):
    # Tested partner TNA uses city + keyword. city is a search hint, NOT a verified administrative constraint.
    if phase=='tna':return {'city':reg['sido'].removesuffix('특별시').removesuffix('광역시').removesuffix('특별자치도').removesuffix('특별자치시').removesuffix('도'), 'keyword':kw,'page':page}
    return {'keyword':kw,'checkIn':checkin,'checkOut':checkout,'adultCount':adults,'page':page}


def execute(con,phase,max_calls,max_pages,delay):
    load_env(); calls=0; stopped=False
    jobs=con.execute('''SELECT job_id,region_json,keyword,checkin,checkout,adults,next_page,status,pages
      FROM nationwide_jobs WHERE phase=? AND status IN ('PENDING','PARTIAL','RETRY') ORDER BY rowid''',(phase,)).fetchall()
    for job_id,region_json,kw,ci,co,adults,page,state,pages_done in jobs:
        if calls>=max_calls or stopped:break
        region=json.loads(region_json)
        per_job=0
        while per_job<max_pages and calls<max_calls:
            if page>=200:
                with con: con.execute('UPDATE nationwide_jobs SET status=?,error=?,updated_at=? WHERE job_id=?',('LIMIT_REACHED','200페이지 안전 상한',now(),job_id))
                break
            p=payload(phase,region,kw,page,ci,co,adults)
            try:
                resp=request(ENDPOINTS['tna' if phase=='tna' else 'accommodation'],p)
            except Exception as ex:
                with con:con.execute('UPDATE nationwide_jobs SET status=?,error=?,updated_at=? WHERE job_id=?',('RETRY',str(ex)[:300],now(),job_id))
                stopped=True;print(f'네트워크 오류: {ex}');break
            calls+=1
            status=resp['http_status']
            if status!=200:
                nextstate='RETRY' if status in (429,500,502,503,504) else 'ERROR'
                with con:con.execute('UPDATE nationwide_jobs SET status=?,error=?,updated_at=? WHERE job_id=?',(nextstate,f'HTTP {status}',now(),job_id))
                print(f'{region["query"]} / {kw} / HTTP {status} => {nextstate}')
                if status in (401,403,429):stopped=True
                break
            obj=resp.get('body',{});data=obj.get('data') if isinstance(obj,dict) else None
            if not isinstance(data,dict) or not isinstance(data.get('items'),list) or any(not isinstance(x,dict) for x in data['items']):
                with con:con.execute('UPDATE nationwide_jobs SET status=?,error=?,updated_at=? WHERE job_id=?',('ERROR','Unknown data.items schema',now(),job_id))
                stopped=True;break
            if data.get('page')!=page:
                with con:con.execute('UPDATE nationwide_jobs SET status=?,error=?,updated_at=? WHERE job_id=?',('ERROR','Returned page mismatch',now(),job_id))
                stopped=True;break
            items=data['items']; ids={product_id(x) for x in items}
            existing={x[0] for x in con.execute('SELECT provider_id FROM nationwide_seen WHERE job_id=?',(job_id,))}
            newids=ids-existing;overlap=ids&existing
            # Raw response is retained even when the search is incomplete.
            saved=save_probe(con,'tna' if phase=='tna' else 'accommodation',kw,reg_to_search_label(region),resp)
            with con:
                for pid in ids:
                    con.execute('INSERT OR IGNORE INTO nationwide_seen(job_id,provider_id) VALUES(?,?)',(job_id,pid))
                    con.execute('''INSERT OR REPLACE INTO nationwide_product_observations(phase,provider_id,job_id,source_region_json,query_keyword,observed_at)
                    VALUES(?,?,?,?,?,?)''',(phase,pid,job_id,region_json,kw,now()))
                con.execute('''INSERT OR REPLACE INTO nationwide_pages(job_id,page,response_path,http_status,returned,new_ids,overlap_ids,reported_total,has_next,fetched_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?)''',(job_id,page,saved['response_path'],200,len(items),len(newids),len(overlap),data.get('totalCount'),str(data.get('hasNextPage')),now()))
                # Search is not declared complete if totalCount differs from unique IDs.
                count=con.execute('SELECT COUNT(*) FROM nationwide_seen WHERE job_id=?',(job_id,)).fetchone()[0]
                has_next=data.get('hasNextPage')
                ended=(has_next is False or not items)
                total=data.get('totalCount')
                complete='END_WITH_GAPS' if ended and isinstance(total,int) and count<total else 'END_OBSERVED'
                newstatus=complete if ended else 'PARTIAL'
                con.execute('''UPDATE nationwide_jobs SET next_page=?,status=?,error=NULL,pages=pages+1,returned=returned+?,unique_seen=?,updated_at=? WHERE job_id=?''',
                  (page+1,newstatus,len(items),count,now(),job_id))
            print(f'{phase} {region["query"]} [{kw}] page={page} rows={len(items)} new={len(newids)} overlap={len(overlap)} status={newstatus}')
            page+=1;per_job+=1
            if ended:break
            # Avoid rapid repeated requests. The time window for rate-limit headers is not established.
            if calls<max_calls:time.sleep(max(1,delay))
    print(f'이번 API 호출 {calls}회, 상태/상품: python src/nationwide_collect.py status')


def reg_to_search_label(r):
    # Label is only the query source, never a verified item address.
    return r['query']


def status(con):
    for phase in ('tna','stays'):
        total=con.execute('SELECT COUNT(*) FROM nationwide_jobs WHERE phase=?',(phase,)).fetchone()[0]
        stages=dict(con.execute('SELECT status,COUNT(*) FROM nationwide_jobs WHERE phase=? GROUP BY status',(phase,)).fetchall())
        unique=con.execute('SELECT COUNT(DISTINCT provider_id) FROM nationwide_product_observations WHERE phase=?',(phase,)).fetchone()[0]
        print(f'{phase}: jobs={total} states={stages} unique_observed={unique}')
    print('경고: unique_observed는 전국 상품의 완전한 수가 아니라 현재 검색에서 관측한 고유 ID 수입니다.')


def main(argv=None):
    ap=argparse.ArgumentParser(description='국내 지역 순회형 제휴 검색 수집기; 원본 보존; 검색 지역은 소재지로 취급하지 않음')
    sub=ap.add_subparsers(dest='cmd',required=True)
    sub.add_parser('init-regions');sub.add_parser('status')
    p=sub.add_parser('plan');p.add_argument('--phase',choices=['tna','stays'],required=True)
    p.add_argument('--depths',default='1,2',help='1=시도,2=시군구,3=읍면동. 기본 1,2')
    p.add_argument('--terms',default='',help='콤마로 구분; 공란이면 기본 상품 종류 키워드')
    p.add_argument('--checkin');p.add_argument('--checkout');p.add_argument('--adults',type=int,default=2)
    r=sub.add_parser('run');r.add_argument('--phase',choices=['tna','stays'],required=True)
    r.add_argument('--max-calls',type=int,default=20);r.add_argument('--max-pages-per-job',type=int,default=5)
    r.add_argument('--delay',type=float,default=2.0)
    a=ap.parse_args(argv)
    if a.cmd=='init-regions':
        regions=fetch_regions();print('지역 목록 다운로드:',REGION_PATH,' / 시도',sum(x['depth']==1 for x in regions),'시군구',sum(x['depth']==2 for x in regions),'읍면동',sum(x['depth']==3 for x in regions),'(원천에 읍면동 없음)');return
    con=get_db();setup(con)
    try:
        if a.cmd=='status':status(con)
        elif a.cmd=='plan':
            depths={int(x) for x in a.depths.split(',')}
            if not depths or not depths.issubset({1,2,3}):raise ValueError('--depths 1,2,3 중 선택')
            terms=tuple(x.strip() for x in a.terms.split(',') if x.strip()) or (DEFAULT_TNA_TERMS if a.phase=='tna' else DEFAULT_STAY_TERMS)
            plan(con,a.phase,depths,terms,a.checkin,a.checkout,a.adults)
        elif a.cmd=='run':
            if not 1<=a.max_calls<=100 or not 1<=a.max_pages_per_job<=100 or a.delay<1:raise ValueError('안전 호출 범위 초과')
            execute(con,a.phase,a.max_calls,a.max_pages_per_job,a.delay)
    finally:con.close()
if __name__=='__main__':main()
