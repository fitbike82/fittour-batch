"""Enrich ONLY already collected MyRealTrip products. Python 3.10+, stdlib.
Requires installed src/mrt_analyzer.py and src/discover_api.py from earlier packages.
Never modifies existing affiliate_products.raw_json or existing affiliate_links.
"""
from __future__ import annotations
import argparse, collections, csv, datetime as dt, json, pathlib, re, sqlite3, sys, time, traceback, urllib.error, urllib.parse

ROOT=pathlib.Path(__file__).resolve().parents[2]
# Package imports use relative paths.
from .api_client import get_db, load_env, request, ENDPOINTS, now, existing_affiliate_link
from .mcp_client import MCPClient
from . import repository as history
import uuid
OUT=ROOT/'data'/'enrichment'

def decode_mcp(raw):
    if isinstance(raw,dict):
        if isinstance(raw.get('structuredContent'),(dict,list)):return raw['structuredContent']
        for part in raw.get('content',[]):
            if isinstance(part,dict) and part.get('type')=='text':
                try:return json.loads(part.get('text',''))
                except (ValueError,TypeError):continue
    return raw

def tool_error(raw,parsed):
    if isinstance(raw,dict) and raw.get('isError'):return str(raw.get('content',''))[:300]
    if isinstance(parsed,dict) and (parsed.get('success') is False or parsed.get('error')):
        return str(parsed.get('error') or parsed.get('message') or 'success=false')[:300]
    return None

def setup(con):
    con.executescript('''
    CREATE TABLE IF NOT EXISTS enrichment_runs (
      product_id INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
      attempts INTEGER NOT NULL DEFAULT 0, requested_at TEXT, updated_at TEXT NOT NULL,
      response_path TEXT, error TEXT, PRIMARY KEY(product_id,kind));
    CREATE TABLE IF NOT EXISTS enrichment_details (
      product_id INTEGER NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
      payload_json TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY(product_id,kind));
    CREATE INDEX IF NOT EXISTS idx_enrichment_status ON enrichment_runs(kind,status);
    ''');con.commit()

def rows(con,kind=None):
    if kind:return con.execute('SELECT id,provider_product_id,product_type,title,product_url,raw_json FROM affiliate_products WHERE product_type=? ORDER BY id',(kind,)).fetchall()
    return con.execute('SELECT id,provider_product_id,product_type,title,product_url,raw_json FROM affiliate_products ORDER BY id').fetchall()

def ensure_jobs(con, include_options=False, include_links=False):
    stamp=now();counts=collections.Counter()
    for p in rows(con):
        pid,remote,kind,title,url,_=p
        if kind not in ('tna','accommodation'):continue
        jobs=['detail']
        if include_options and kind=='tna':jobs.append('options')
        if include_links:jobs.append('link')
        for job in jobs:
            con.execute('INSERT OR IGNORE INTO enrichment_runs(product_id,kind,status,updated_at) VALUES(?,?,?,?)',(pid,job,'PENDING',stamp));counts[job]+=1
    con.commit();return counts

def write_json(product_id,kind,obj):
    path=OUT/'raw'/kind/str(product_id)/(dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'_'+uuid.uuid4().hex+'.json');path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(path)
    try:return str(path.relative_to(ROOT))
    except ValueError:return str(path)

def mark(con,pid,kind,status,response=None,error=None,source=None):
    path=None
    if response is not None:
        path=write_json(pid,kind,response)
    with con:
        document=response if response is not None else {'error':error}
        history.record(con,pid,kind,source or kind,document,document.get('request',{}) if isinstance(document,dict) else {},now(),path,document.get('http_status') if isinstance(document,dict) else None,status,error,event_key=uuid.uuid4().hex)
        con.execute('''UPDATE enrichment_runs SET status=?, attempts=attempts+1, requested_at=?,updated_at=?,response_path=COALESCE(?,response_path),error=? WHERE product_id=? AND kind=?''', (status,now(),now(),path,error,pid,kind))
        if response is not None and status=='SUCCESS':
            con.execute('''INSERT INTO enrichment_details(product_id,kind,source,payload_json,fetched_at) VALUES(?,?,?,?,?)
              ON CONFLICT(product_id,kind) DO UPDATE SET payload_json=excluded.payload_json,fetched_at=excluded.fetched_at''',
              (pid,kind,source or kind,json.dumps(response,ensure_ascii=False),now()))

def link_from_body(body):
    from .link_policy import extract
    return extract(body)

def existing_link(con,pid,url,placement):
    return existing_affiliate_link(con,pid,url,placement)

def derive_price(v):
    if isinstance(v,dict):
        for k in ('salePrice','sale_price','discountPrice','price'):
            if v.get(k) is not None:return v[k]
    return None

def act(con,job,product,mcp,opts):
    pid,remote,typ,title,url,raws=product
    kind=job
    if kind=='link':
        if not url:
            mark(con,pid,kind,'SKIPPED',error='product_url missing');return 'SKIPPED'
        old=existing_link(con,pid,url,opts.placement)
        if old:
            mark(con,pid,kind,'SUCCESS',response={'affiliate_url':old[0],'reused':True},source='existing_link');return 'SUCCESS'
        response=request(ENDPOINTS['mylink'],{'targetUrl':url})
        if response['http_status']!=200:
            mark(con,pid,kind,'RETRY' if response['http_status'] in (429,500,502,503,504) else 'ERROR',response=response,error=f"HTTP {response['http_status']}",source='mylink');return 'RETRY' if response['http_status'] in (429,500,502,503,504) else 'ERROR'
        link=link_from_body(response['body'])
        if not link:
            mark(con,pid,kind,'ERROR',response=response,error='No verified myrealt.rip URL in response',source='mylink');return 'ERROR'
        with con:
            con.execute('''INSERT OR IGNORE INTO affiliate_links(affiliate_company_id,affiliate_product_id,target_url,affiliate_url,placement,created_at)
             VALUES(1,?,?,?,?,?)''',(pid,url,link,opts.placement,now()))
        mark(con,pid,kind,'SUCCESS',response={'response':response,'affiliate_url':link},source='mylink');return 'SUCCESS'
    if not re.fullmatch(r'\d+',str(remote)):
        mark(con,pid,kind,'SKIPPED',error='Not a numeric provider ID');return 'SKIPPED'
    if kind=='detail' and typ=='tna':
        if not url:
            mark(con,pid,kind,'SKIPPED',error='TNA requires source URL');return 'SKIPPED'
        tool='getTnaDetail';params={'gid':str(remote),'url':url}
    elif kind=='detail' and typ=='accommodation':
        tool='getStayDetail';params={'gid':int(remote),'checkIn':opts.check_in,'checkOut':opts.check_out,'adultCount':opts.adults}
    elif kind=='options' and typ=='tna':
        if not url:
            mark(con,pid,kind,'SKIPPED',error='TNA options requires URL');return 'SKIPPED'
        tool='getTnaOptions';params={'gid':str(remote),'url':url,'selectedDate':opts.option_date}
    else:
        mark(con,pid,kind,'SKIPPED',error='Unsupported product kind combination');return 'SKIPPED'
    raw=mcp.call(tool,params);parsed=decode_mcp(raw);err=tool_error(raw,parsed)
    document={'tool':tool,'request':params,'raw_result':raw,'parsed_content':parsed,'fetched_at':now()}
    if err:
        status=('UNSUPPORTED' if kind=='options' and 'No CALENDAR component found' in err
                else 'RETRY' if any(x in err.lower() for x in ('429','rate limit','too many requests','temporarily unavailable')) else 'ERROR')
        mark(con,pid,kind,status,response=document,error=err,source=tool);return status
    mark(con,pid,kind,'SUCCESS',response=document,source=tool);return 'SUCCESS'

def get_work(con,opts):
    product_map={p[0]:p for p in rows(con)}
    where=['r.status IN (\'PENDING\',\'RETRY\')']
    if opts.retry_errors:where=['r.status IN (\'PENDING\',\'RETRY\',\'ERROR\')']
    if opts.kind!='all':where.append('r.kind=?')
    if opts.product_type!='all':where.append('p.product_type=?')
    args=[]
    if opts.kind!='all':args.append(opts.kind)
    if opts.product_type!='all':args.append(opts.product_type)
    q='''SELECT r.product_id,r.kind,r.status FROM enrichment_runs r JOIN affiliate_products p ON p.id=r.product_id
      WHERE '''+' AND '.join(where)+''' ORDER BY CASE r.kind WHEN 'detail' THEN 0 WHEN 'options' THEN 1 ELSE 2 END,r.product_id'''
    return [(pid,kind,st,product_map[pid]) for pid,kind,st in con.execute(q,args).fetchall()]

def run(con,opts):
    if opts.kind=='link' and not opts.confirm_links:raise SystemExit('실제 링크 생성은 --confirm-links 옵션이 필요합니다.')
    jobs=get_work(con,opts);print('대상 작업',len(jobs),'최대 호출',opts.max_calls)
    mcp=None;mcpcalls=0;used=0;outcomes=collections.Counter()
    for pid,kind,old,product in jobs:
        if used>=opts.max_calls:break
        if kind=='link' and not opts.confirm_links:continue
        if kind!='link' and mcp is None:
            try:mcp=MCPClient();mcp.start();tools={t['name'] for t in mcp.list_tools()}
            except Exception as e:print('MCP 연결 실패:',repr(e));break
            missing={'getTnaDetail','getStayDetail','getTnaOptions'}-tools
            if missing:print('주의: 미노출 MCP 도구',sorted(missing))
        try:
            status=act(con,kind,product,mcp,opts)
        except urllib.error.HTTPError as e:
            code=e.code;status='RETRY' if code in (429,500,502,503,504) else 'ERROR'
            mark(con,pid,kind,status,response={'http_status':code,'message':str(e)},error=str(e))
            if code in (401,403,429):
                print(f'HTTP {code} 접근/호출 제한. 진행 상태 저장 후 즉시 중단');outcomes[status]+=1;used+=1;break
        except Exception as e:
            msg=f'{type(e).__name__}: {e}'
            status='RETRY' if any(x in msg.lower() for x in ('429','timed out','timeout','temporar','503','502')) else 'ERROR'
            mark(con,pid,kind,status,error=msg[:400]);print('요청 실패:',pid,kind,msg[:150])
            if '429' in msg:outcomes[status]+=1;used+=1;break
        outcomes[status]+=1;used+=1
        print(f'{used}/{opts.max_calls} product={pid} type={product[2]} job={kind} status={status}')
        if kind=='link' and status=='ERROR':
            error=con.execute('SELECT error FROM enrichment_runs WHERE product_id=? AND kind=?',(pid,kind)).fetchone()
            if error and error[0] in ('HTTP 401','HTTP 403'):
                print('인증/접근 거부: 다른 상품의 반복 발급을 중단합니다.');break
        if status=='RETRY':
            print('일시적 오류/호출 제한 감지: 체크포인트 보존 후 중단');break
        if opts.delay>0 and used<opts.max_calls:time.sleep(opts.delay)
    print('이번 요청',used,'결과',dict(outcomes));status_report(con)

def status_report(con):
    print('상품:',dict(con.execute('SELECT product_type,COUNT(*) FROM affiliate_products GROUP BY product_type').fetchall()))
    for kind,status,count in con.execute('SELECT kind,status,COUNT(*) FROM enrichment_runs GROUP BY kind,status ORDER BY kind,status'):
        print(f'  {kind:8s} {status:12s} {count}')
    total=con.execute('SELECT COUNT(*) FROM affiliate_links WHERE affiliate_company_id=1').fetchone()[0]
    print('기존+발급 링크 전체:',total)

def flatten(v,prefix='',depth=0):
    if depth>14:return {}
    if isinstance(v,dict):
        d={}
        for k,x in v.items():d.update(flatten(x,prefix+'.'+k if prefix else k,depth+1))
        return d if d else {prefix:'{}'}
    if isinstance(v,list):
        # Full array is retained in raw JSON; human-readable CSV includes JSON arrays, not thousands of indexed columns.
        return {prefix:json.dumps(v,ensure_ascii=False)}
    return {prefix: v if v is None or isinstance(v,(str,int,float,bool)) else str(v)}

def export(con):
    dest=OUT/'export';dest.mkdir(parents=True,exist_ok=True)
    products=rows(con)
    details={(r[0],r[1]):json.loads(r[2]) for r in con.execute('SELECT product_id,kind,payload_json FROM enrichment_details')}
    status={(r[0],r[1]):(r[2],r[3],r[4]) for r in con.execute('SELECT product_id,kind,status,error,response_path FROM enrichment_runs')}
    links=collections.defaultdict(list)
    product_url_to_id={p[4]:p[0] for p in products if p[4]}
    for dbid,url,target,placement in con.execute('SELECT affiliate_product_id,affiliate_url,target_url,placement FROM affiliate_links WHERE affiliate_company_id=1'):
        resolved=dbid if dbid is not None else product_url_to_id.get(target)
        if resolved is not None:links[resolved].append((url,target,placement))
    observed=[];fields=set();coverage=collections.Counter();field_counts=collections.Counter()
    for pid,remote,typ,title,url,raws in products:
        try:basic=json.loads(raws) if raws else {}
        except ValueError:basic={}
        row={'db_id':pid,'affiliate_company_id':1,'provider_product_id':remote,'product_type':typ,'title':title,'product_url':url,
             'affiliate_url':links[pid][0][0] if links[pid] else '',
             'affiliate_links_json':json.dumps([{'url':x[0],'target_url':x[1],'placement':x[2]} for x in links[pid]],ensure_ascii=False),
             'detail_status':status.get((pid,'detail'),('NOT_PLANNED',None))[0],
             'detail_error':status.get((pid,'detail'),(None,None))[1] or '',
             'options_status':status.get((pid,'options'),('NOT_PLANNED',None))[0],
             'link_status':status.get((pid,'link'),('NOT_PLANNED',None,None))[0],
             'detail_response_path':status.get((pid,'detail'),(None,None,None))[2] or '',
             'options_response_path':status.get((pid,'options'),(None,None,None))[2] or '',
             'link_response_path':status.get((pid,'link'),(None,None,None))[2] or '',
             'domestic_verification':'UNVERIFIED',
             'search_raw_json':json.dumps(basic,ensure_ascii=False),
             'detail_raw_json':json.dumps(details.get((pid,'detail')),ensure_ascii=False) if (pid,'detail') in details else '',
             'options_raw_json':json.dumps(details.get((pid,'options')),ensure_ascii=False) if (pid,'options') in details else ''}
        for k,v in flatten(basic).items():row['search.'+k]=v
        detail=details.get((pid,'detail'))
        if detail is not None:
            parsed=detail.get('parsed_content',detail)
            for k,v in flatten(parsed).items():row['detail.'+k]=v
            if typ=='accommodation' and isinstance(parsed,dict):
                loc=parsed.get('location') or {};row['verified_address_from_mcp']=loc.get('address','');row['latitude_from_mcp']=loc.get('latitude','');row['longitude_from_mcp']=loc.get('longitude','')
        options=details.get((pid,'options'))
        if options is not None:
            for k,v in flatten(options.get('parsed_content',options)).items():row['options.'+k]=v
        observed.append(row);fields.update(row.keys())
        for k,v in row.items():
            if v is not None and v!='':field_counts[k]+=1
        coverage[(typ,row['detail_status'])]+=1
    fixed=['db_id','affiliate_company_id','provider_product_id','product_type','title','product_url','affiliate_url','affiliate_links_json',
           'detail_status','detail_error','options_status','link_status','detail_response_path','options_response_path','link_response_path','domestic_verification','verified_address_from_mcp','latitude_from_mcp','longitude_from_mcp']
    cols=fixed+sorted(fields-set(fixed)-{'search_raw_json','detail_raw_json','options_raw_json'})
    def csv_write(name,columns,data):
        path=dest/name
        with path.open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore');w.writeheader();w.writerows(data)
        return path
    # master omits giant raw JSON; full JSONL has all raw and flattened values.
    path=csv_write('product_master_all_columns.csv',cols,observed)
    with (dest/'product_master_full.jsonl').open('w',encoding='utf-8') as f:
        for row in observed:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    csv_write('field_coverage.csv',['column','rows_present','total_rows','percent'],[
        {'column':k,'rows_present':field_counts[k],'total_rows':len(observed),'percent':round(field_counts[k]/len(observed)*100,2) if observed else 0} for k in sorted(fields)])
    info={'generated_at':now(),'total_products':len(products),'by_type':dict(collections.Counter(p[2] for p in products)),
          'detail_by_type_and_status':{f'{k[0]}.{k[1]}':v for k,v in coverage.items()},
          'linked_product_count':sum(bool(links[x[0]]) for x in products),
          'notes':['search keyword is not verified location','full raw search/detail/options JSON is retained in JSONL and DB','unknown or unavailable fields are not invented']}
    (dest/'summary.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
    print('내보내기:',path,' / ',dest/'product_master_full.jsonl',' / ',dest/'field_coverage.csv');print(json.dumps(info,ensure_ascii=False,indent=2))

def main():
    p=argparse.ArgumentParser(description='Enrich existing MRT inventory, without restarting nationwide search')
    s=p.add_subparsers(dest='command',required=True)
    a=s.add_parser('plan');a.add_argument('--options',action='store_true');a.add_argument('--links',action='store_true')
    b=s.add_parser('run');b.add_argument('--kind',choices=['all','detail','options','link'],default='detail');b.add_argument('--product-type',choices=['all','tna','accommodation'],default='all');b.add_argument('--max-calls',type=int,default=20);b.add_argument('--delay',type=float,default=2.0);b.add_argument('--retry-errors',action='store_true');b.add_argument('--confirm-links',action='store_true');b.add_argument('--placement',default='default')
    today=dt.date.today();b.add_argument('--check-in',default=(today+dt.timedelta(days=21)).isoformat());b.add_argument('--check-out',default=(today+dt.timedelta(days=22)).isoformat());b.add_argument('--option-date',default=(today+dt.timedelta(days=21)).isoformat());b.add_argument('--adults',type=int,default=2)
    s.add_parser('status');s.add_parser('export')
    a=p.parse_args();con=get_db();setup(con)
    if a.command=='plan':print('생성/확인 작업:',dict(ensure_jobs(con,a.options,a.links)));status_report(con)
    elif a.command=='run':
        if not 1<=a.max_calls<=10000:raise SystemExit('--max-calls must be 1..10000')
        if a.delay<0:raise SystemExit('--delay must be nonnegative')
        if a.check_out<=a.check_in:raise SystemExit('--check-out must be later than --check-in')
        load_env();run(con,a)
    elif a.command=='status':status_report(con)
    else:export(con)

if __name__=='__main__':main()
