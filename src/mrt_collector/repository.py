"""Additive, backed-up SQLite migration and append-only provider observations."""
from __future__ import annotations
import csv
import datetime as dt
import hashlib
import json
import pathlib
import sqlite3
import uuid
from contextlib import closing
from .models import canonical, digest

ROOT = pathlib.Path(__file__).resolve().parents[2]
ACTIVE_RUN = None


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def tables(con):
    return [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def quote(name):
    return '"'+name.replace('"','""')+'"'


def snapshot(con, names=None):
    result={}
    for name in names or tables(con):
        rows=[canonical(list(r)) for r in con.execute('SELECT * FROM '+quote(name))]
        result[name]={'count':len(rows),'sha256':hashlib.sha256('\n'.join(sorted(rows)).encode()).hexdigest()}
    return result


def inspect(con):
    result={'integrity_check':con.execute('PRAGMA integrity_check').fetchone()[0],
            'foreign_key_check':[list(r) for r in con.execute('PRAGMA foreign_key_check')],
            'tables':{}}
    for name in tables(con):
        result['tables'][name]={'columns':[list(r) for r in con.execute('PRAGMA table_info('+quote(name)+')')],
            'foreign_keys':[list(r) for r in con.execute('PRAGMA foreign_key_list('+quote(name)+')')],
            'indexes':[{'info':list(r),'columns':[list(c) for c in con.execute('PRAGMA index_info('+quote(r[1])+')')]} for r in con.execute('PRAGMA index_list('+quote(name)+')')],
            **snapshot(con,[name])[name]}
    result['duplicates']=[list(r) for r in con.execute('SELECT affiliate_company_id,product_type,provider_product_id,COUNT(*) FROM affiliate_products GROUP BY 1,2,3 HAVING COUNT(*)>1')]
    result['missing']={k:con.execute('SELECT COUNT(*) FROM affiliate_products WHERE '+quote(k)+" IS NULL OR "+quote(k)+"='' ").fetchone()[0] for k in ('provider_product_id','title','product_url','raw_json')}
    for name,q in {'product_types':'SELECT product_type,COUNT(*) FROM affiliate_products GROUP BY 1',
                   'enrichment':'SELECT kind,status,COUNT(*) FROM enrichment_runs GROUP BY 1,2',
                   'nationwide':'SELECT phase,status,COUNT(*) FROM nationwide_jobs GROUP BY 1,2'}.items():
        if (name=='enrichment' and 'enrichment_runs' not in tables(con)) or (name=='nationwide' and 'nationwide_jobs' not in tables(con)):continue
        result[name]=[list(r) for r in con.execute(q)]
    return result


def backup_database(db_path, backup_dir):
    db_path=pathlib.Path(db_path);backup_dir=pathlib.Path(backup_dir)
    backup_dir.mkdir(parents=True,exist_ok=True)
    dest=backup_dir/(db_path.stem+'-before-model-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.sqlite3')
    with closing(sqlite3.connect(db_path.as_uri()+'?mode=ro',uri=True)) as source, closing(sqlite3.connect(dest)) as target:
        source.backup(target)
        target.commit()
        if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Backup integrity failed')
        # A stable read snapshot ensures WAL-backed content is compared consistently.
        source.execute('BEGIN')
        before=snapshot(source)
        if snapshot(target)!=before:raise RuntimeError('Backup content mismatch; no migration performed')
    return dest,before


def apply_schema(con):
    """No destructive SQL, no implicit executescript commit. Caller owns transaction."""
    sql=(ROOT/'db/migrations/001_observations.sql').read_text(encoding='utf-8')
    pending=''
    for line in sql.splitlines(True):
        pending+=line
        if sqlite3.complete_statement(pending):
            con.execute(pending);pending=''
    con.execute('INSERT OR IGNORE INTO collector_migrations VALUES(1,?)',(stamp(),))


def ready(con):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_observations'").fetchone())


PRICE_KEYS={'salePrice','originalPrice','discountPrice','price','sale_price','original_price','priceDisplay','displayPrice','totalPrice','nightlyPrice'}


def walk(value, prefix='$'):
    yield prefix,value
    if isinstance(value,dict):
        for key,child in value.items():yield from walk(child,prefix+'.'+key)
    elif isinstance(value,list):
        for i,child in enumerate(value):yield from walk(child,f'{prefix}[{i}]')


def record(con, product_id, kind, source, payload, request=None, observed_at=None,
           response_path=None, http_status=None, status='SUCCESS', error=None, event_key=None):
    if not ready(con):return None
    req=request or {}
    key=event_key or digest([product_id,kind,source,observed_at,req,payload,response_path,status,error])
    con.execute('''INSERT OR IGNORE INTO product_observations
        (event_key,product_id,kind,source,observed_at,request_json,payload_json,response_path,http_status,status,error,run_id,imported_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (key,product_id,kind,source,observed_at,canonical(req),canonical(payload),str(response_path) if response_path else None,http_status,status,error,ACTIVE_RUN,stamp()))
    oid=con.execute('SELECT id FROM product_observations WHERE event_key=?',(key,)).fetchone()[0]
    if product_id is None:return oid
    # Only structured provider prices. Widget text is kept intact, never parsed into invented amounts.
    parsed=payload.get('parsed_content',payload) if isinstance(payload,dict) else payload
    for p,node in walk(parsed):
        if not isinstance(node,dict):continue
        for k,v in node.items():
            if k in PRICE_KEYS and v is not None:
                con.execute('''INSERT OR IGNORE INTO product_prices
                 (observation_id,product_id,json_path,value_json,currency,observed_at,request_json,source)
                 VALUES(?,?,?,?,?,?,?,?)''',(oid,product_id,p+'.'+k,canonical(v),node.get('currency') if isinstance(node.get('currency'),str) else None,observed_at,canonical(req),source))
        if any(k in node for k in ('address','latitude','longitude')):
            con.execute('''INSERT OR IGNORE INTO product_locations
             (observation_id,product_id,json_path,place_name,country,city,address,latitude_json,longitude_json,source,payload_json)
             VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(oid,product_id,p,node.get('name') if isinstance(node.get('name'),str) else None,
             node.get('country') if isinstance(node.get('country'),str) else None,node.get('city') if isinstance(node.get('city'),str) else None,
             node.get('address') if isinstance(node.get('address'),str) else None,canonical(node['latitude']) if 'latitude' in node else None,
             canonical(node['longitude']) if 'longitude' in node else None,source,canonical(node)))
    return oid


def backfill(con, root):
    """Import historical files without rewriting any master or checkpoint rows."""
    root=pathlib.Path(root);issues=[];contexts={}
    # Recover historical price request conditions only where a persisted job proves them.
    existing=tables(con)
    if 'collection_pages' in existing:
        for path,page,raw in con.execute('SELECT p.response_file,p.requested_page,j.job_json FROM collection_pages p JOIN collection_jobs j ON j.job_key=p.job_key'):
            job=json.loads(raw)
            context={'keyword':job.get('keyword'),'page':page}
            if job.get('type')=='tna':context['city']=job.get('city')
            else:context.update(checkIn=job.get('checkin'),checkOut=job.get('checkout'),adultCount=job.get('adults'))
            if path:contexts[path.replace('\\','/')]=context
    if 'stay44_observations' in existing:
        for path,kw,ci,co,adults,page in con.execute('SELECT raw_response_path,keyword,checkin,checkout,adults,requested_page FROM stay44_observations'):
            if path:contexts[path.replace('\\','/')]={'keyword':kw,'checkIn':ci,'checkOut':co,'adultCount':adults,'page':page}
    if 'nationwide_pages' in existing:
        for path,page,phase,region,kw,ci,co,adults in con.execute('SELECT p.response_path,p.page,j.phase,j.region_json,j.keyword,j.checkin,j.checkout,j.adults FROM nationwide_pages p JOIN nationwide_jobs j ON j.job_id=p.job_id'):
            context={'keyword':kw,'page':page,'search_region':json.loads(region)}
            if phase=='stays':context.update(checkIn=ci,checkOut=co,adultCount=adults)
            if path:contexts[path.replace('\\','/')]=context
    products={}
    for pid,remote,typ,raw,at,kw,city in con.execute('SELECT id,provider_product_id,product_type,raw_json,last_seen_at,source_keyword,source_city FROM affiliate_products'):
        products[(typ,str(remote))]=pid
        record(con,pid,'search','legacy_master',json.loads(raw),{'keyword':kw,'city':city,'conditions_unknown':True},at,event_key=digest(['legacy_master',pid,raw]))
    if 'enrichment_details' in tables(con):
        for pid,kind,source,raw,at in con.execute('SELECT product_id,kind,source,payload_json,fetched_at FROM enrichment_details'):
            obj=json.loads(raw)
            record(con,pid,kind,source,obj,obj.get('request',{}),at,event_key=digest(['legacy_detail',pid,kind,raw]))
    # Import every JSON document in raw-data trees, including failed and older observations.
    # Keep files as byte-preserving authorities; DB JSON preserves their complete parsed content.
    for folder in ('data/responses','data/enrichment/raw','data/diagnostics','results'):
        for file in sorted((root/folder).rglob('*.json')):
            rel=file.relative_to(root).as_posix()
            try:obj=json.loads(file.read_text(encoding='utf-8-sig'))
            except (ValueError,UnicodeError) as exc:
                issues.append({'file':rel,'error':str(exc)});continue
            at=obj.get('at') or obj.get('fetched_at') or obj.get('requested_at') if isinstance(obj,dict) else None
            source=obj.get('path') or obj.get('tool') or 'legacy_file' if isinstance(obj,dict) else 'legacy_file'
            req=(obj.get('request') or contexts.get(rel,{})) if isinstance(obj,dict) else contexts.get(rel,{})
            response=obj.get('body',{}) if isinstance(obj,dict) else {}
            record(con,None,'raw',source,obj,req,at,rel,obj.get('http_status') if isinstance(obj,dict) else None,
                   'ERROR' if isinstance(obj,dict) and obj.get('http_status',200)!=200 else 'SUCCESS',event_key=digest(['raw_file',rel,obj]))
            items=response.get('data',{}).get('items',[]) if isinstance(response,dict) and isinstance(response.get('data'),dict) else []
            for i,item in enumerate(items):
                if not isinstance(item,dict):continue
                remote=next((str(item[k]) for k in ('itemId','productId','product_id','gid','id') if item.get(k) is not None),None)
                candidates=[(typ,pid) for (typ,r),pid in products.items() if r==remote]
                if len(candidates)!=1:continue
                typ,pid=candidates[0]
                # The source response itself does not always contain its request conditions.
                context=dict(req) if isinstance(req,dict) else {'request':req}
                context.setdefault('conditions_unknown',not bool(req))
                record(con,pid,'search',source,item,context,at,rel,event_key=digest(['file_item',rel,i,item]))
            if '/enrichment/raw/' in '/'+rel:
                try:pid=int(file.stem);kind=file.parent.name
                except ValueError:continue
                if pid not in products.values():continue
                state='ERROR' if isinstance(obj,dict) and obj.get('http_status',200)!=200 else 'SUCCESS'
                if isinstance(obj,dict) and isinstance(obj.get('parsed_content'),dict) and obj['parsed_content'].get('success') is False:state='ERROR'
                code=obj.get('http_status') if isinstance(obj,dict) else None
                record(con,pid,kind,source,obj,req,at,rel,http_status=code,status=state,
                       error=('HTTP '+str(code)) if code is not None and code!=200 else None,
                       event_key=digest(['enrichment_file',rel,obj]))
    return issues


def migrate(db_path, backup_dir=None):
    db_path=pathlib.Path(db_path).resolve()
    backup,before=backup_database(db_path,backup_dir or db_path.parent/'backups')
    with closing(sqlite3.connect(db_path)) as con:
        con.execute('PRAGMA foreign_keys=ON')
        if snapshot(con,list(before))!=before:raise RuntimeError('Database changed after backup; stop concurrent writers')
        con.execute('BEGIN IMMEDIATE')
        try:
            apply_schema(con)
            evidence_root=db_path.parent.parent if db_path.parent.name=='data' else db_path.parent
            issues=backfill(con,evidence_root)
            after=snapshot(con,list(before))
            if before!=after:raise RuntimeError('Existing row preservation failed')
            if con.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Integrity failed')
            if list(con.execute('PRAGMA foreign_key_check')):raise RuntimeError('Foreign key check failed')
            con.commit()
        except BaseException:
            con.rollback();raise
        report={'backup':str(backup),'before':before,'after':after,'preserved':before==after,'import_issues':issues,'audit':inspect(con)}
    dest=db_path.parent/'migration_report.json'
    dest.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


def export_tables(con,dest):
    """Lossless relational transfer: all tables, all columns, CSV plus JSONL."""
    dest=pathlib.Path(dest);dest.mkdir(parents=True,exist_ok=True)
    manifest={}
    for name in tables(con):
        cur=con.execute('SELECT * FROM '+quote(name));cols=[d[0] for d in cur.description]
        count=0
        with (dest/(name+'.jsonl')).open('w',encoding='utf-8') as jf,(dest/(name+'.csv')).open('w',encoding='utf-8-sig',newline='') as cf:
            writer=csv.writer(cf);writer.writerow(cols)
            for row in cur:
                jf.write(canonical(dict(zip(cols,row)))+'\n')
                # CSV NULL uses explicit marker; JSONL is the authoritative typed representation.
                writer.writerow(['\\N' if x is None else x for x in row]);count+=1
        manifest[name]={'columns':cols,'rows':count}
    (dest/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    return manifest


def run_start(con,command,context=None):
    global ACTIVE_RUN
    ACTIVE_RUN=uuid.uuid4().hex
    with con:con.execute('INSERT INTO collector_runs(run_id,command,started_at,status,context_json) VALUES(?,?,?,?,?)',(ACTIVE_RUN,command,stamp(),'RUNNING',canonical(context or {})))
    return ACTIVE_RUN


def run_end(con,status):
    global ACTIVE_RUN
    if ACTIVE_RUN:
        with con:con.execute('UPDATE collector_runs SET ended_at=?,status=? WHERE run_id=?',(stamp(),status,ACTIVE_RUN))
    ACTIVE_RUN=None
