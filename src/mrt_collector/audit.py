"""Read-only audit of MyRealTrip local collection, with optional bounded API schema probes.
Python 3.10+ stdlib. Existing data and schema remain unchanged.
"""
from __future__ import annotations
import argparse, collections, csv, datetime as dt, hashlib, json, pathlib, sqlite3, sys, time
from .api_client import DATA, ENDPOINTS, get_db, load_env, product_id, product_url, request

OUT=DATA/'collector_audit'
PREFERRED={'tna':['gid','itemName','description','category','tags','salePrice','priceDisplay','reviewScore','reviewCount','imageUrl','productUrl','deepLink'],
 'accommodation':['itemId','itemName','salePrice','originalPrice','starRating','reviewScore','reviewCount','imageUrl','productUrl','deepLink']}
MATCH_FIELDS=('address','roadAddress','jibunAddress','zipCode','postalCode','latitude','longitude','lat','lng','lon','phone','telephone','contact','city','region','regionCode','district','areaCode','contentId')

def make_hash(x):return hashlib.sha256(json.dumps(x,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
def field_paths(obj,prefix=''):
    if isinstance(obj,dict):
        for k,v in obj.items():
            p=f'{prefix}.{k}' if prefix else k
            yield p
            yield from field_paths(v,p)
    elif isinstance(obj,list):
        yield prefix+'[]'
        for v in obj: yield from field_paths(v,prefix+'[]')

def paths_with_values(obj,prefix=''):
    if isinstance(obj,dict):
        for k,v in obj.items():
            p=f'{prefix}.{k}' if prefix else k
            if isinstance(v,(dict,list)):yield from paths_with_values(v,p)
            else:yield p,v
    elif isinstance(obj,list):
        if not obj:yield prefix+'[]',None
        for v in obj:yield from paths_with_values(v,prefix+'[]')
    else:
        yield prefix,obj

def is_value(v):return v is not None and v!='' and v!=[] and v!={}
def safe_list(x):return x if isinstance(x,list) else []
def report_for_rows(rows):
    by_kind=collections.defaultdict(list)
    for kind,r in rows:by_kind[kind].append(r)
    out={}
    for kind,group in sorted(by_kind.items()):
        counters=collections.Counter();present=collections.Counter(); value_types=collections.defaultdict(set)
        for r in group:
            paths=dict(paths_with_values(r))
            for p,v in paths.items():
                counters[p]+=1
                if is_value(v):present[p]+=1
                value_types[p].add(type(v).__name__)
        out[kind]={'products':len(group),'fields':[{ 'path':p,'observed':counters[p], 'nonempty':present[p], 'nonempty_pct':round(present[p]*100/len(group),2),'types':sorted(value_types[p])} for p in sorted(counters)], 'missing_matching_fields':[x for x in MATCH_FIELDS if not any(p.rsplit('.',1)[-1].lower()==x.lower() for p in counters)]}
    return out

def inventory(conn):
    rows=[]; malformed=[]
    q='SELECT id,affiliate_company_id,provider_product_id,product_type,title,product_url,source_keyword,source_city,first_seen_at,last_seen_at,raw_json FROM affiliate_products ORDER BY id'
    for rid,cid,pid,kind,title,url,keyword,city,first,last,raw in conn.execute(q):
        try:r=json.loads(raw)
        except (TypeError,ValueError):malformed.append(rid);continue
        if not isinstance(r,dict):malformed.append(rid);continue
        rows.append(({'db_id':rid,'affiliate_company_id':cid,'provider_product_id':pid,'product_type':kind,'title':title,'product_url':url,'source_keyword':keyword,'source_city_search_hint_only':city,'first_seen_at':first,'last_seen_at':last},r))
    return rows,malformed

def write_csv(path,rows,cols):
    with path.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=cols,extrasaction='ignore');w.writeheader();w.writerows(rows)

def audit(db_path=None,outdir=OUT):
    outdir=pathlib.Path(outdir);outdir.mkdir(parents=True,exist_ok=True)
    if db_path:
        conn=sqlite3.connect(f'file:{pathlib.Path(db_path).resolve().as_posix()}?mode=ro',uri=True)
    else:
        if not (DATA/'inventory.sqlite3').is_file():raise FileNotFoundError('data/inventory.sqlite3 없음')
        conn=sqlite3.connect(f'file:{(DATA/"inventory.sqlite3").resolve().as_posix()}?mode=ro',uri=True)
    try:
        rows,invalid=inventory(conn)
        typed=[(m['product_type'],r) for m,r in rows]
        types=report_for_rows(typed)
        with (outdir/'all_products_full.jsonl').open('w',encoding='utf-8') as f:
            for meta,raw in rows:f.write(json.dumps({'source':meta,'all_api_fields':raw},ensure_ascii=False)+'\n')
        summary=[]; field_rows=[]
        for kind,item in types.items():
            for field in item['fields']: field_rows.append({'type':kind,**field})
        for m,raw in rows:
            summary.append({**m,'description':raw.get('description'),'category':raw.get('category'),
                'tags':json.dumps(raw.get('tags'),ensure_ascii=False),'sale_price':raw.get('salePrice'),
                'original_price':raw.get('originalPrice'),'image_url':raw.get('imageUrl'),
                'review_score':raw.get('reviewScore'),'review_count':raw.get('reviewCount'),
                'location_verified':int(any(is_value(v) and p.rsplit('.',1)[-1].lower() in ('latitude','longitude','address','roadaddress','jibunaddress') for p,v in paths_with_values(raw)))})
        write_csv(outdir/'product_inventory.csv',summary,list(summary[0]) if summary else ['db_id'])
        write_csv(outdir/'field_inventory.csv',field_rows,['type','path','observed','nonempty','nonempty_pct','types'])
        probes=[];pcols=[]
        if 'api_probe_runs' in {x[0] for x in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            probes=[dict(zip(('endpoint','keyword','city','status_code','elapsed_ms','result_count','collection_path','response_path','recorded_at'),r)) for r in conn.execute('SELECT endpoint,keyword,city,status_code,elapsed_ms,result_count,collection_path,response_path,recorded_at FROM api_probe_runs ORDER BY id')]
        jobs=[]
        if 'collection_jobs' in {x[0] for x in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            cur=conn.execute('SELECT * FROM collection_jobs');cols=[x[0] for x in cur.description];jobs=[dict(zip(cols,r)) for r in cur.fetchall()]
        sessions=[]
        if 'stay44_sessions' in {x[0] for x in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            cur=conn.execute('SELECT * FROM stay44_sessions'); cols=[x[0] for x in cur.description];sessions=[dict(zip(cols,r)) for r in cur.fetchall()]
        result={'generated_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'mode':'READ_ONLY','total_products':len(rows),'invalid_raw_records':invalid,
                'types':types,'probe_count':len(probes),'probe_history':probes,'collection_jobs':jobs,'stay_sessions':sessions,
                'limitations':['Full nationwide coverage not proven','Search region is not verified place location','Detail API not verified','Image/data retention permission unverified','Price validity depends on query dates/party size','Past overwritten raw_json versions cannot be reconstructed from current inventory'],
                'outputs':['all_products_full.jsonl','product_inventory.csv','field_inventory.csv','final_audit.json']}
        (outdir/'final_audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        print('AUDIT: products=',len(rows),'types=',{k:v['products'] for k,v in types.items()},'invalid=',len(invalid))
        print('FILES:',outdir)
        return result
    finally:conn.close()

def probe(args):
    """Explicit, limited fresh checks only on already confirmed search endpoints."""
    load_env(); out=pathlib.Path(args.out);out.mkdir(parents=True,exist_ok=True)
    start=dt.date.fromisoformat(args.checkin);end=dt.date.fromisoformat(args.checkout)
    if end<=start:raise ValueError('checkout must be later than checkin')
    if not 1<=args.max_calls<=8:raise ValueError('max-calls range 1..8')
    requests=[]
    if args.kind in ('tna','both'):
        for page in (1,2):requests.append(('tna',{'keyword':args.tna_keyword,'city':args.city,'page':page}))
    if args.kind in ('accommodation','both'):
        for page in (0,1):requests.append(('accommodation',{'keyword':args.stay_keyword,'checkIn':args.checkin,'checkOut':args.checkout,'adultCount':args.adults,'page':page}))
    results=[]
    for idx,(kind,payload) in enumerate(requests[:args.max_calls]):
        resp=request(ENDPOINTS[kind],payload)
        stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        filename=f'{stamp}_{kind}_page_{payload["page"]}.json'
        (out/filename).write_text(json.dumps({'request':payload,'response':resp},ensure_ascii=False,indent=2),encoding='utf-8')
        data=resp.get('body',{}).get('data',{})
        items=safe_list(data.get('items')) if isinstance(data,dict) else []
        unique=set(product_id(i) for i in items if isinstance(i,dict))
        row={'type':kind,'page_requested':payload['page'],'http':resp['http_status'],'page_returned':data.get('page') if isinstance(data,dict) else None,'items':len(items),'unique_ids':len(unique),'totalCount':data.get('totalCount') if isinstance(data,dict) else None,'hasNextPage':data.get('hasNextPage') if isinstance(data,dict) else None,'perPage_or_size':data.get('perPage',data.get('size')) if isinstance(data,dict) else None,'fields':sorted(set(p for item in items if isinstance(item,dict) for p in field_paths(item))),'headers':resp['headers'],'file':filename}
        results.append(row);print(kind,'page',payload['page'],'HTTP',row['http'],'items',row['items'],'unique',row['unique_ids'])
        if resp['http_status']!=200:break
        if idx+1<min(args.max_calls,len(requests)):time.sleep(args.delay)
    comparisons=[]
    for kind in ('tna','accommodation'):
        rr=[r for r in results if r['type']==kind]
        if len(rr)>=2:
            a=json.loads((out/rr[0]['file']).read_text(encoding='utf-8'))['response']['body']['data']['items'];b=json.loads((out/rr[1]['file']).read_text(encoding='utf-8'))['response']['body']['data']['items']
            A={product_id(x) for x in a};B={product_id(x) for x in b}
            comparisons.append({'type':kind,'two_page_overlap':len(A&B),'jaccard':round(len(A&B)/len(A|B),4) if A|B else None})
    report={'mode':'BOUNDED_LIVE_READ_ONLY','queries':results,'comparisons':comparisons,'note':'A successful two-page check does not prove exhaustive coverage or API quota interval.'}
    (out/'live_schema_probe.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report

def main():
    p=argparse.ArgumentParser(description='MRT final collector audit, no matching/AI/frontend')
    s=p.add_subparsers(dest='mode',required=True)
    a=s.add_parser('audit',help='Read-only audit existing SQLite records')
    a.add_argument('--db');a.add_argument('--out',default=str(OUT))
    b=s.add_parser('probe',help='Bounded, read-only search API schema probes')
    b.add_argument('--kind',choices=['tna','accommodation','both'],default='both')
    b.add_argument('--city',default='서울');b.add_argument('--tna-keyword',default='입장권')
    b.add_argument('--stay-keyword',default='제주 호텔');b.add_argument('--checkin',default='2026-12-10');b.add_argument('--checkout',default='2026-12-11');b.add_argument('--adults',type=int,default=2)
    b.add_argument('--max-calls',type=int,default=4);b.add_argument('--delay',type=float,default=2.0);b.add_argument('--out',default=str(OUT/'live_probe'))
    args=p.parse_args()
    if args.mode=='audit':audit(args.db,args.out)
    else:probe(args)
if __name__=='__main__':main()
