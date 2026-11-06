"""Read-only discovery of partner search schemas and official public MCP tool schemas.
Python 3.10+ standard library. Never sends credentials to the public MCP server.
"""
import argparse, collections, datetime, json, os, pathlib, re, sys, urllib.error, urllib.request, uuid, time
ROOT=pathlib.Path(__file__).resolve().parents[2]
DEFAULT_OUT=ROOT/'results'
MCP='https://mcp-servers.myrealtrip.com/mcp'
API='https://partner-ext-api.myrealtrip.com'
SECRETS=re.compile(r'(api.?key|authorization|password|cookie|secret|access.?token|refresh.?token)',re.I)

def clean(obj):
    from .api_client import redact
    return redact(obj)

def save(out,name,data):
    out.mkdir(parents=True,exist_ok=True)
    p=out/name;p.write_text(json.dumps(clean(data),ensure_ascii=False,indent=2),encoding='utf-8');return p

def utc():return datetime.datetime.now(datetime.timezone.utc).isoformat()

def load_env():
    from .api_client import load_env as load
    return load()

def flatten(x,prefix=''):
    if isinstance(x,dict):
        result=[]
        for k,v in x.items():result += flatten(v,prefix+'.'+k if prefix else k)
        return result or [prefix]
    if isinstance(x,list):
        result=[prefix+'[]'];
        for v in x:result += flatten(v,prefix+'[]')
        return result
    return [prefix]

def audit_local(source,out):
    rows={'tna':[],'accommodation':[]};envelopes=[]
    if source.is_file() and source.suffix=='.zip':
        import zipfile
        with zipfile.ZipFile(source) as z:
            sources=[(n,json.loads(z.read(n))) for n in z.namelist() if n.endswith('.json')]
    else:
        sources=[]
        for p in source.rglob('*.json'):
            try:sources.append((str(p),json.loads(p.read_text('utf-8'))))
            except (ValueError,UnicodeError):pass
    for name,doc in sources:
        body=doc.get('body',doc) if isinstance(doc,dict) else {}
        if not isinstance(body,dict):continue
        data=body.get('data',{})
        if not isinstance(data,dict) or not isinstance(data.get('items'),list):continue
        kind='tna' if ('tna' in name or 'hasNextPage' in data) else 'accommodation'
        rows[kind].extend([x for x in data['items'] if isinstance(x,dict)])
        envelopes.append({'file':name,'kind':kind,'response_data_keys':list(data.keys()),'meta_keys':list(body.get('meta',{})) if isinstance(body.get('meta'),dict) else [],'items':len(data['items'])})
    summary={'generated_at':utc(),'source':str(source),'response_samples':envelopes,'product_types':{}}
    for kind,items in rows.items():
        counts=collections.Counter(p for r in items for p in set(flatten(r)))
        summary['product_types'][kind]={'records':len(items),'fields':[{ 'path':p,'present':c,'share':round(c/len(items),4) if items else 0} for p,c in sorted(counts.items())], 'category_values': dict(collections.Counter(str(r.get('category')) for r in items if r.get('category') is not None))}
    save(out,'local_response_field_inventory.json',summary)
    return summary

def json_result(raw,content_type):
    text=raw.decode('utf-8','replace')
    if 'text/event-stream' in content_type or text.lstrip().startswith('event:') or text.lstrip().startswith('data:'):
        objs=[]
        for line in text.splitlines():
            if line.startswith('data:'):
                payload=line[5:].strip()
                if payload=='[DONE]':continue
                try:objs.append(json.loads(payload))
                except ValueError:pass
        if not objs:raise ValueError('SSE에서 JSON 메시지를 읽지 못했습니다.')
        return objs[-1]
    return json.loads(text)

def persist_exchange(method,params,status,body):
    from . import repository as history
    import sqlite3
    document={'method':method,'request':params,'http_status':status,'body':body,'at':utc()}
    directory=ROOT/'data/responses';directory.mkdir(parents=True,exist_ok=True)
    path=directory/('mcp_exchange_'+uuid.uuid4().hex+'.json')
    path.write_text(json.dumps(clean(document),ensure_ascii=False,indent=2),encoding='utf-8')
    db=ROOT/'data/inventory.sqlite3'
    if db.exists():
        with sqlite3.connect(db) as con:
            history.record(con,None,'mcp_response',method,clean(document),params,document['at'],str(path.relative_to(ROOT)),status,'SUCCESS' if status==200 else 'ERROR',event_key=uuid.uuid4().hex)


class MCPClient:
    def __init__(self, url=MCP):self.url=url;self.session=None;self.protocol='2025-03-26'
    def post(self,method,params=None,notify=False):
        msg={'jsonrpc':'2.0','method':method}
        if not notify:msg['id']=uuid.uuid4().hex
        if params is not None:msg['params']=params
        headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream','MCP-Protocol-Version':self.protocol,'User-Agent':'fittour-api-discovery/1.0'}
        if self.session:headers['Mcp-Session-Id']=self.session
        req=urllib.request.Request(self.url,data=json.dumps(msg).encode(),headers=headers,method='POST')
        try:
            with urllib.request.urlopen(req,timeout=30) as res:
                raw=res.read()
                if res.headers.get('Mcp-Session-Id'):self.session=res.headers.get('Mcp-Session-Id')
                ct=res.headers.get('Content-Type','')
                status=res.status
        except urllib.error.HTTPError as exc:
            persist_exchange(method,params,exc.code,{'non_json_body':exc.read().decode('utf-8','replace')})
            raise
        persist_exchange(method,params,status,{'raw_text':raw.decode('utf-8','replace')})
        if notify:return {}
        obj=json_result(raw,ct)
        if 'error' in obj:raise RuntimeError(str(obj['error']))
        return obj.get('result',obj)
    def start(self):
        info=self.post('initialize',{'protocolVersion':self.protocol,'capabilities':{},'clientInfo':{'name':'fittour-api-discovery','version':'1.0'}})
        self.post('notifications/initialized',notify=True)
        return info
    def list_tools(self):
        tools=[];cursor=None;seen=set()
        for _ in range(20):
            params={'cursor':cursor} if cursor else None
            result=self.post('tools/list',params)
            tools.extend(result.get('tools',[]))
            cursor=result.get('nextCursor')
            if not cursor or cursor in seen:break
            seen.add(cursor)
        return tools
    def call(self,name,args):
        result=self.post('tools/call',{'name':name,'arguments':args})
        folder=ROOT/'data/responses';folder.mkdir(parents=True,exist_ok=True)
        document={'tool':name,'request':args,'raw_result':result,'at':utc()}
        p=folder/('mcp_'+uuid.uuid4().hex+'.json');p.write_text(json.dumps(document,ensure_ascii=False,indent=2),encoding='utf-8')
        return result

def mcp_discover(out,auto_categories=False,detail_calls=None):
    report={'generated_at':utc(),'endpoint':MCP,'status':'FAILED'}
    try:
        client=MCPClient();report['server_info']=client.start();tools=client.list_tools();report['status']='OK';report['tool_count']=len(tools)
        report['tools']=[{'name':t.get('name'),'description':t.get('description'),'inputSchema':t.get('inputSchema'),'outputSchema':t.get('outputSchema')} for t in tools]
        save(out,'mcp_tool_schemas.json',report)
        if auto_categories:
            category=[t for t in tools if t.get('name','').lower()=='getcategorylist']
            if not category:report['category_query']='NOT_AVAILABLE'
            else:
                t=category[0];required=t.get('inputSchema',{}).get('required',[])
                if required:report['category_query']={'status':'REQUIRES_ARGUMENTS','required':required,'schema':t.get('inputSchema')}
                else:
                    try:
                        result=client.call(t['name'],{});save(out,'mcp_category_list_raw.json',result);report['category_query']='OK'
                    except Exception as e:report['category_query']=f'{type(e).__name__}: {e}'
        for spec in detail_calls or []:
            name,args=spec
            if name not in {t['name'] for t in tools}:raise ValueError(f'등록되지 않은 MCP 도구: {name}')
            result=client.call(name,args)
            save(out,f'mcp_call_{re.sub("[^a-zA-Z0-9_-]","_",name)}_{uuid.uuid4().hex[:8]}.json',{'tool':name,'arguments':args,'result':result})
        save(out,'mcp_discovery_status.json',{k:v for k,v in report.items() if k!='tools'})
    except Exception as e:
        report['error']=f'{type(e).__name__}: {e}'
        save(out,'mcp_discovery_status.json',report)
    return report

def partner_probe(out,kind,payload):
    load_env();key=os.getenv('MRT_API_KEY','')
    if not key:raise RuntimeError('MRT_API_KEY 가 .env 또는 환경 변수에 없습니다.')
    path={'tna':'/v1/products/tna/search','accommodation':'/v1/products/accommodation/search'}[kind]
    headers={'Authorization':'Bearer '+key,'Content-Type':'application/json','Accept':'application/json'}
    req=urllib.request.Request(API+path,data=json.dumps(payload,ensure_ascii=False).encode('utf-8'),headers=headers,method='POST')
    try:
        with urllib.request.urlopen(req,timeout=30) as resp:status=resp.status;body=resp.read(6_000_000);resp_headers=dict(resp.headers)
    except urllib.error.HTTPError as e:status=e.code;body=e.read(2_000_000);resp_headers=dict(e.headers)
    try:parsed=json.loads(body)
    except (UnicodeError,ValueError):parsed={'raw_excerpt':body[:300].decode('utf-8','replace')}
    record={'at':utc(),'endpoint':path,'request':payload,'status':status,'rate_headers':{k:v for k,v in resp_headers.items() if re.search('rate|limit|retry',k,re.I)},'body':parsed}
    file=f'partner_{kind}_{uuid.uuid4().hex[:10]}.json';save(out,file,record)
    data=parsed.get('data',{}) if isinstance(parsed,dict) else {}
    items=data.get('items',[]) if isinstance(data,dict) else []
    return {'http':status,'items':len(items) if isinstance(items,list) else None,'data_keys':list(data.keys()) if isinstance(data,dict) else [],'saved':file,'note':'HTTP 200은 요청 필터가 적용되었다는 증거가 아닙니다.'}

def main():
    ap=argparse.ArgumentParser(description='API capability discovery; read-only')
    ap.add_argument('--out',default=str(DEFAULT_OUT))
    sub=ap.add_subparsers(dest='cmd',required=True)
    lp=sub.add_parser('local-fields');lp.add_argument('--source',required=True)
    mp=sub.add_parser('mcp-discover');mp.add_argument('--categories',action='store_true',help='getCategoryList 입력값이 불필요할 때만 호출')
    cp=sub.add_parser('mcp-call');cp.add_argument('--tool',required=True);cp.add_argument('--args-json',default='{}')
    pp=sub.add_parser('partner-probe');pp.add_argument('--kind',choices=['tna','accommodation'],required=True);pp.add_argument('--payload-json',required=True)
    args=ap.parse_args();out=pathlib.Path(args.out)
    if args.cmd=='local-fields':
        result=audit_local(pathlib.Path(args.source),out)
        print('응답 파일:',len(result['response_samples']),'상품별:',{k:v['records'] for k,v in result['product_types'].items()})
    elif args.cmd=='mcp-discover':
        result=mcp_discover(out,args.categories);print('MCP',result['status'],'tools',result.get('tool_count'),'category',result.get('category_query'),result.get('error',''))
    elif args.cmd=='mcp-call':
        result=mcp_discover(out,False,[(args.tool,json.loads(args.args_json))]);print('MCP',result['status'],result.get('error',''))
    elif args.cmd=='partner-probe':print(partner_probe(out,args.kind,json.loads(args.payload_json)))
    print('결과 폴더:',out)
if __name__=='__main__':main()
