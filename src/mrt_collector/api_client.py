"""MyRealTrip partner API discovery and local inventory. Python 3.10+, stdlib only."""
from __future__ import annotations
import argparse, csv, datetime as dt, hashlib, json, os, pathlib, re, sqlite3, sys, time, urllib.error, urllib.request, urllib.parse, uuid
from . import repository as history

ROOT = pathlib.Path(__file__).resolve().parents[2]
DATA = ROOT / 'data'
API_BASE = 'https://partner-ext-api.myrealtrip.com'
ENDPOINTS = {'tna':'/v1/products/tna/search','accommodation':'/v1/products/accommodation/search','mylink':'/v1/mylink'}
SENSITIVE = re.compile(r'(authorization|api.?key|secret|token|password|cookie)', re.I)

def load_env():
    p = ROOT / '.env'
    if not p.exists(): return
    for line in p.read_text(encoding='utf-8-sig').splitlines():
        line=line.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        k,v=line.split('=',1)
        if k.strip() not in os.environ: os.environ[k.strip()]=v.strip().strip('"').strip("'")

def redact(obj):
    if isinstance(obj,dict): return {k:('[REDACTED]' if SENSITIVE.search(k) else redact(v)) for k,v in obj.items()}
    if isinstance(obj,list): return [redact(x) for x in obj]
    return obj

def now(): return dt.datetime.now(dt.timezone.utc).isoformat()

def history_status(code):
    from .models import http_state
    return http_state(code)


def request(path, payload, method='POST'):
    key=os.environ.get('MRT_API_KEY','').strip()
    if not key: raise ValueError('MRT_API_KEY가 설정되지 않았습니다. .env.example을 .env로 복사하세요.')
    url=API_BASE+path
    body=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None
    req=urllib.request.Request(url,data=body,headers={'Authorization':'Bearer '+key,'Content-Type':'application/json','Accept':'application/json','User-Agent':'mrt-affiliate-analyzer/0.1'},method=method)
    t0=time.monotonic()
    try:
        with urllib.request.urlopen(req,timeout=25) as r:
            status=r.status; raw=r.read(); headers=dict(r.headers.items())
    except urllib.error.HTTPError as e:
        status=e.code; raw=e.read(); headers=dict(e.headers.items())
    elapsed=round((time.monotonic()-t0)*1000)
    try: result=json.loads(raw.decode('utf-8'))
    except (UnicodeError,json.JSONDecodeError): result={'non_json_excerpt':raw[:500].decode('utf-8','replace'),'non_json_body':raw.decode('utf-8','replace')}
    safe_headers={k:v for k,v in headers.items() if re.search(r'(rate|limit|retry|content-type|request-id)',k,re.I)}
    response={'at':now(),'path':path,'http_status':status,'elapsed_ms':elapsed,'headers':redact(safe_headers),'body':redact(result),'request':redact(payload)}
    # Every real API attempt, including retry/error, gets its own immutable response.
    folder=DATA/'responses';folder.mkdir(parents=True,exist_ok=True)
    saved=folder/(dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'_'+uuid.uuid4().hex+'.json')
    saved.write_text(json.dumps(response,ensure_ascii=False,indent=2),encoding='utf-8')
    db=DATA/'inventory.sqlite3'
    if db.exists():
        with sqlite3.connect(db) as con:
            history.record(con,None,'api_response',path,response,payload,response['at'],str(saved.relative_to(ROOT)),status,
                           history_status(status),event_key=uuid.uuid4().hex)
    return response

def flatten_paths(node,prefix='',depth=0):
    if depth>9: return []
    if isinstance(node,dict):
        out=[]
        for k,v in node.items(): out.extend(flatten_paths(v,f'{prefix}.{k}' if prefix else k,depth+1))
        return out
    if isinstance(node,list):
        return [prefix+'[]']+([x for x in flatten_paths(node[0],prefix+'[]',depth+1)] if node else [])
    return [prefix]

def list_candidates(body):
    """Heuristic; never assumes a pagination or response schema."""
    candidates=[]
    def walk(x,path='',depth=0):
        if depth>6:return
        if isinstance(x,list):
            if x and isinstance(x[0],dict): candidates.append((path,x))
            return
        if isinstance(x,dict):
            for k,v in x.items():walk(v,(path+'.'+k).strip('.'),depth+1)
    walk(body)
    return max(candidates,key=lambda t:len(t[1]),default=('',[]))

def product_url(row):
    for k in ('productUrl','product_url','url','detailUrl','link'):
        if isinstance(row.get(k),str) and row[k].startswith('https://'):return row[k]
    return None

def product_id(row):
    for k in ('itemId','productId','product_id','gid','id'):
        if row.get(k) is not None:return str(row[k])
    url=product_url(row)
    if url:return 'urlsha:'+hashlib.sha256(url.encode()).hexdigest()[:20]
    return 'rowsha:'+hashlib.sha256(json.dumps(row,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:20]

def init_db(con):
    con.executescript('''
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS affiliate_companies (
       id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
       integration_type TEXT NOT NULL DEFAULT 'API', api_base_url TEXT, is_active INTEGER NOT NULL DEFAULT 1
    );
    INSERT OR IGNORE INTO affiliate_companies(id,code,name,api_base_url) VALUES
       (1,'MYREALTRIP','마이리얼트립','https://partner-ext-api.myrealtrip.com');
    CREATE TABLE IF NOT EXISTS affiliate_products (
       id INTEGER PRIMARY KEY, affiliate_company_id INTEGER NOT NULL REFERENCES affiliate_companies(id),
       provider_product_id TEXT NOT NULL, product_type TEXT NOT NULL, title TEXT, product_url TEXT,
       source_keyword TEXT, source_city TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
       raw_json TEXT NOT NULL, UNIQUE(affiliate_company_id,provider_product_id,product_type)
    );
    CREATE TABLE IF NOT EXISTS affiliate_links (
       id INTEGER PRIMARY KEY, affiliate_company_id INTEGER NOT NULL REFERENCES affiliate_companies(id),
       affiliate_product_id INTEGER REFERENCES affiliate_products(id), target_url TEXT NOT NULL,
       affiliate_url TEXT NOT NULL, placement TEXT NOT NULL DEFAULT 'default', created_at TEXT NOT NULL,
       UNIQUE(affiliate_company_id,target_url,placement)
    );
    CREATE TABLE IF NOT EXISTS api_probe_runs (
       id INTEGER PRIMARY KEY, endpoint TEXT NOT NULL, keyword TEXT, city TEXT,
       status_code INTEGER NOT NULL, elapsed_ms INTEGER, result_count INTEGER,
       collection_path TEXT, fields_json TEXT, response_path TEXT, recorded_at TEXT NOT NULL
    );
    ''')
    con.commit()

def get_db():
    DATA.mkdir(exist_ok=True)
    con=sqlite3.connect(DATA/'inventory.sqlite3');init_db(con);return con

def save_probe(con,kind,keyword,city,response):
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    path=DATA/'responses'/f'{stamp}_{kind}.json';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(response,ensure_ascii=False,indent=2),encoding='utf-8')
    collection_path,rows=list_candidates(response['body']) if response['http_status']==200 else ('',[])
    fields=sorted(set(p for row in rows[:10] for p in flatten_paths(row)))
    con.execute('INSERT INTO api_probe_runs(endpoint,keyword,city,status_code,elapsed_ms,result_count,collection_path,fields_json,response_path,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(kind,keyword,city,response['http_status'],response['elapsed_ms'],len(rows),collection_path,json.dumps(fields,ensure_ascii=False),str(path.relative_to(ROOT)),response['at']))
    if response['http_status']==200:
        for row in rows:
            pid=product_id(row);title=str(next((row[k] for k in ('itemName','title','name','productName') if row.get(k)),''))
            con.execute('''INSERT INTO affiliate_products(affiliate_company_id,provider_product_id,product_type,title,product_url,source_keyword,source_city,first_seen_at,last_seen_at,raw_json)
            VALUES(1,?,?,?,?,?,?,?,?,?) ON CONFLICT(affiliate_company_id,provider_product_id,product_type)
            DO UPDATE SET last_seen_at=excluded.last_seen_at,raw_json=excluded.raw_json,title=excluded.title,
            product_url=COALESCE(excluded.product_url,affiliate_products.product_url)''',
            (pid,kind,title,product_url(row),keyword,city,response['at'],response['at'],json.dumps(row,ensure_ascii=False)))
    if response['http_status']==200:
        for row in rows:
            local=con.execute('SELECT id FROM affiliate_products WHERE affiliate_company_id=1 AND provider_product_id=? AND product_type=?',(product_id(row),kind)).fetchone()
            if local:
                history.record(con,local[0],'search',ENDPOINTS[kind],row,response.get('request') or {'keyword':keyword,'city':city,'conditions_unknown':True},response['at'],str(path.relative_to(ROOT)),response['http_status'])
    con.commit()
    return {'status':response['http_status'],'count':len(rows),'collection_path':collection_path,'field_count':len(fields),'response_path':str(path.relative_to(ROOT)),'elapsed_ms':response['elapsed_ms']}

def do_probe(args):
    load_env();con=get_db();summary=[]
    jobs=[('tna',{'keyword':args.tna_keyword,'city':args.city})]
    if args.include_stays:
        start=dt.date.fromisoformat(args.checkin) if args.checkin else dt.date.today()+dt.timedelta(days=30)
        end=dt.date.fromisoformat(args.checkout) if args.checkout else start+dt.timedelta(days=1)
        if end<=start:raise ValueError('checkout은 checkin 이후여야 합니다.')
        jobs.append(('accommodation',{'keyword':args.stay_keyword,'checkIn':start.isoformat(),'checkOut':end.isoformat(),'adultCount':args.adults}))
    for ix,(kind,payload) in enumerate(jobs):
        try:resp=request(ENDPOINTS[kind],payload)
        except Exception as e:
            print(f'{kind}: 요청 실패 ({type(e).__name__}: {e})',file=sys.stderr);continue
        result=save_probe(con,kind,payload.get('keyword'),payload.get('city'),resp)
        summary.append({'endpoint':kind,'input':payload,**result})
        print(f'{kind}: HTTP {result["status"]}, 검출 배열 {result["count"]}건, 경로={result["collection_path"] or "미확인"}')
        if result['status'] in (401,403):break
        if ix<len(jobs)-1:time.sleep(args.delay)
    (DATA/'last_probe_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print('보고서: data/last_probe_summary.json  DB: data/inventory.sqlite3')

def do_report(args):
    con=get_db(); rows=con.execute('SELECT endpoint,keyword,city,status_code,elapsed_ms,result_count,collection_path,recorded_at FROM api_probe_runs ORDER BY id DESC LIMIT 100').fetchall()
    cols=['endpoint','keyword','city','status','latency_ms','detected_items','array_path','recorded_at']
    with (DATA/'probe_report.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.writer(f);w.writerow(cols);w.writerows(rows)
    products=con.execute('SELECT product_type,COUNT(*) FROM affiliate_products GROUP BY product_type').fetchall()
    print('API 검사:',len(rows),'회 | 저장 상품:',dict(products))
    for row in rows[:10]:print(dict(zip(cols,row)))
    print('CSV: data/probe_report.csv')

def existing_affiliate_link(con, product_id, target_url, placement='default'):
    from .link_policy import existing
    return existing(con,product_id,target_url,placement)


def do_mylink(args):
    if not args.confirm:raise ValueError('제휴 링크를 실제 생성합니다. --confirm을 추가해야 합니다.')
    load_env();con=get_db()
    p=con.execute('SELECT id,product_url FROM affiliate_products WHERE id=?',(args.product_db_id,)).fetchone()
    if not p or not p[1]:raise ValueError('상품 ID 또는 product_url이 없습니다. report/inventory에서 확인하세요.')
    existing=existing_affiliate_link(con,p[0],p[1],args.placement)
    if existing:print('이미 생성된 링크:',existing[0]);return
    resp=request(ENDPOINTS['mylink'],{'targetUrl':p[1]})
    if resp['http_status']>=300:
        print('생성 실패:',resp['http_status'],json.dumps(resp['body'],ensure_ascii=False)[:600]);return
    body=resp['body']
    from .link_policy import extract
    out=extract(body)
    if not out:
        print('HTTP 성공. 하지만 링크 필드 자동 식별 불가. 응답 키를 확인하세요:',list(body.keys()) if isinstance(body,dict) else type(body).__name__);return
    con.execute('INSERT INTO affiliate_links(affiliate_company_id,affiliate_product_id,target_url,affiliate_url,placement,created_at) VALUES(1,?,?,?,?,?)',(p[0],p[1],out,args.placement,now()));con.commit();print('생성 및 저장:',out)

def main():
    ap=argparse.ArgumentParser(description='마이리얼트립 제휴 API 로컬 분석기 (운영 DB 미연동)')
    sub=ap.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('probe',help='레저/티켓 + 선택적 숙소 요청을 안전한 소량으로 검사')
    p.add_argument('--city',default='서울');p.add_argument('--tna-keyword',default='입장권');p.add_argument('--include-stays',action='store_true');p.add_argument('--stay-keyword',default='제주 호텔');p.add_argument('--checkin');p.add_argument('--checkout');p.add_argument('--adults',type=int,default=2);p.add_argument('--delay',type=float,default=2.0);p.set_defaults(func=do_probe)
    p=sub.add_parser('report',help='검증 결과 CSV 및 요약');p.set_defaults(func=do_report)
    p=sub.add_parser('mylink',help='선택한 상품 1건만 링크 발급 (명시 확인)');p.add_argument('--product-db-id',type=int,required=True);p.add_argument('--placement',default='default');p.add_argument('--confirm',action='store_true');p.set_defaults(func=do_mylink)
    args=ap.parse_args()
    try:args.func(args)
    except (ValueError,sqlite3.Error) as e: print(f'오류: {e}',file=sys.stderr);sys.exit(2)
if __name__=='__main__':main()
