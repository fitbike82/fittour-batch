"""MCP city/category search using real category evidence and existing checkpoints."""
import argparse
import json
import pathlib
import re
import time
import urllib.error
import urllib.parse
from . import api_client as api, collector, repository as repo
from .enrichment import decode_mcp, tool_error
from .mcp_client import MCPClient
from .models import digest


def plan(con,city,document):
    parsed=document.get('parsed_content') or decode_mcp(document.get('raw_result',document))
    observed_city=document.get('request',{}).get('city') or parsed.get('searchParams',{}).get('city')
    if observed_city!=city or tool_error(document.get('raw_result',{}),parsed):
        raise ValueError('Category evidence must be successful and belong to the requested city')
    categories=parsed.get('categories')
    if not isinstance(categories,list):raise ValueError('Missing categories array; no guessed categories')
    count=0
    with con:
        for category in categories:
            if not isinstance(category,dict) or not isinstance(category.get('value'),str) or not category['value']:continue
            spec={'city':city,'category':category['value'],'category_evidence':document}
            key=digest(['mcp_tna',city,category['value']])[:24]
            con.execute('''INSERT OR IGNORE INTO nationwide_jobs(job_id,phase,region_json,keyword,next_page,status,updated_at)
             VALUES(?,'mcp_tna',?,?,1,'PENDING',?)''',(key,json.dumps(spec,ensure_ascii=False),city,api.now()));count+=1
    return count


def products(parsed):
    """Use only actual product/action URLs; preserve full MCP search content."""
    found={}
    for _,node in repo.walk(parsed):
        if not isinstance(node,dict):continue
        action=node.get('onClickAction',{})
        candidates=[node.get('productUrl'),node.get('url')]
        if isinstance(action,dict):
            candidates.extend([action.get('url'),action.get('payload',{}).get('target',{}).get('url') if isinstance(action.get('payload'),dict) else None])
        for url in candidates:
            if not isinstance(url,str):continue
            p=urllib.parse.urlparse(url)
            match=re.search(r'/products/(\d+)(?:/|$)',p.path)
            if p.scheme=='https' and p.hostname in ('experiences.myrealtrip.com','www.myrealtrip.com','myrealtrip.com') and match:
                gid=match.group(1)
                found[gid]={'gid':gid,'productUrl':url,'mcp_search_raw':parsed}
                for key in ('itemName','name','title','imageUrl','salePrice','originalPrice','currency','category','tags'):
                    if key in node:found[gid][key]=node[key]
    return list(found.values())


def execute(con,client,max_calls=5,delay=30,per_page=20):
    used=0
    jobs=con.execute("SELECT job_id,region_json,next_page FROM nationwide_jobs WHERE phase='mcp_tna' AND status IN ('PENDING','PARTIAL','RETRY') ORDER BY rowid").fetchall()
    for key,raw,page in jobs:
        if used>=max_calls:break
        spec=json.loads(raw);request={'query':spec['city'],'category':spec['category'],'page':page,'perPage':per_page}
        try:
            used+=1;result=client.call('searchTnas',request);parsed=decode_mcp(result);error=tool_error(result,parsed)
            if error:raise RuntimeError(error)
            items=products(parsed)
            pagination=next((node for _,node in repo.walk(parsed) if isinstance(node,dict) and isinstance(node.get('hasNextPage'),bool)),None)
            document={'tool':'searchTnas','request':request,'raw_result':result,'parsed_content':parsed,'fetched_at':api.now()}
            directory=api.DATA/'responses';directory.mkdir(parents=True,exist_ok=True)
            path=directory/('category_'+digest(document)+'.json');path.write_text(json.dumps(document,ensure_ascii=False,indent=2),encoding='utf-8')
            with con:
                for item in items:
                    gid=item['gid'];at=document['fetched_at']
                    con.execute('''INSERT INTO affiliate_products(affiliate_company_id,provider_product_id,product_type,title,product_url,source_keyword,source_city,first_seen_at,last_seen_at,raw_json)
                     VALUES(1,?,'tna',?,?,?,?,?,?,?) ON CONFLICT(affiliate_company_id,provider_product_id,product_type) DO NOTHING''',
                     (gid,item.get('itemName') or item.get('name') or item.get('title'),item['productUrl'],spec['category'],spec['city'],at,at,json.dumps(item,ensure_ascii=False)))
                    pid=con.execute("SELECT id FROM affiliate_products WHERE affiliate_company_id=1 AND product_type='tna' AND provider_product_id=?",(gid,)).fetchone()[0]
                    repo.record(con,pid,'search','mcp:searchTnas',document,request,at,str(path),status='SUCCESS')
                    con.execute('INSERT OR IGNORE INTO nationwide_seen VALUES(?,?)',(key,gid))
                    con.execute("INSERT OR REPLACE INTO nationwide_product_observations VALUES('mcp_tna',?,?,?,?,?)",(gid,key,raw,spec['category'],at))
                state='PARTIAL' if pagination and pagination['hasNextPage'] else 'END_OBSERVED' if pagination else 'NEEDS_REVIEW'
                con.execute('''UPDATE nationwide_jobs SET next_page=?,status=?,pages=pages+1,returned=returned+?,
                 unique_seen=(SELECT COUNT(*) FROM nationwide_seen WHERE job_id=?),updated_at=?,error=? WHERE job_id=?''',
                 (page+1 if pagination else page,state,len(items),key,document['fetched_at'],None if pagination else 'MCP pagination unavailable; completeness unknown',key))
            print(key,state,len(items))
        except Exception as exc:
            code=exc.code if isinstance(exc,urllib.error.HTTPError) else None
            state='RETRY' if code in (429,500,502,503,504) or code is None else 'ERROR'
            with con:con.execute('UPDATE nationwide_jobs SET status=?,error=?,updated_at=? WHERE job_id=?',(state,str(exc)[:400],api.now(),key))
            break
        if used<max_calls:time.sleep(delay)
    return used


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    d=sub.add_parser('discover');d.add_argument('--city',required=True);d.add_argument('--out',type=pathlib.Path,required=True)
    p=sub.add_parser('plan');p.add_argument('--city',required=True);p.add_argument('--category-file',type=pathlib.Path,required=True)
    x=sub.add_parser('run');x.add_argument('--max-calls',type=int,default=5);x.add_argument('--delay',type=float,default=30);x.add_argument('--per-page',type=int,default=20)
    sub.add_parser('status');args=parser.parse_args()
    if args.command=='discover':
        client=MCPClient();client.start();raw=client.call('getCategoryList',{'city':args.city})
        document={'request':{'city':args.city},'raw_result':raw,'parsed_content':decode_mcp(raw)}
        args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(json.dumps(document,ensure_ascii=False,indent=2),encoding='utf-8');return
    con=api.get_db();collector.setup(con)
    try:
        if args.command=='plan':print('category jobs:',plan(con,args.city,json.loads(args.category_file.read_text(encoding='utf-8-sig'))))
        elif args.command=='status':print(con.execute("SELECT status,COUNT(*) FROM nationwide_jobs WHERE phase='mcp_tna' GROUP BY status").fetchall())
        else:
            if not 1<=args.max_calls<=100 or args.delay<1 or not 1<=args.per_page<=100:parser.error('Invalid safe call limits')
            client=MCPClient();client.start();execute(con,client,args.max_calls,args.delay,args.per_page)
    finally:con.close()
