"""One CLI, with legacy arguments forwarded unchanged."""
import argparse
import json
import pathlib
import sqlite3
import sys
from contextlib import closing
from . import repository as repo


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    modes={'search':'collector','category':'category_search','paged-search':'paged_collector','stays':'stays_collector',
           'enrich':'enrichment','links':'affiliate_links','audit':'audit','partner':'api_client','mcp':'mcp_client'}
    parser=argparse.ArgumentParser(description='MRT independent collector; no FitTour matching')
    parser.add_argument('command',choices=['status','migrate','export','db-audit',*modes])
    if not argv or argv[0] in ('-h','--help'):
        parser.print_help();return
    mode=argv.pop(0)
    if mode not in modes and mode not in ('status','migrate','export','db-audit'):
        parser.error('unknown command: '+mode)
    if mode in modes:
        import importlib
        module=importlib.import_module('.'+modes[mode],__package__)
        old=sys.argv
        sys.argv=[module.__file__,*argv]
        con=None
        try:
            db=repo.ROOT/'data/inventory.sqlite3'
            if db.exists() and not any(x in argv for x in ('-h','--help')):
                con=sqlite3.connect(db)
                if repo.ready(con):repo.run_start(con,mode,{'arguments':argv})
            module.main()
            if con:repo.run_end(con,'FINISHED')
        except BaseException:
            if con:repo.run_end(con,'INTERRUPTED_OR_ERROR')
            raise
        finally:
            sys.argv=old
            if con:con.close()
        return
    p=argparse.ArgumentParser(prog='mrt_collector '+mode)
    p.add_argument('--db',type=pathlib.Path,default=repo.ROOT/'data/inventory.sqlite3')
    p.add_argument('--out',type=pathlib.Path,default=repo.ROOT/'data/transfer')
    args=p.parse_args(argv)
    if mode=='migrate':
        result=repo.migrate(args.db)
        print(json.dumps({'preserved':result['preserved'],'backup':result['backup'],'import_issues':result['import_issues']},ensure_ascii=False,indent=2));return
    with closing(sqlite3.connect(args.db.resolve().as_uri()+'?mode=ro',uri=True)) as con:
        if mode=='export':result=repo.export_tables(con,args.out)
        elif mode=='db-audit':
            result=repo.inspect(con);args.out.mkdir(parents=True,exist_ok=True)
            (args.out/'db_audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        else:
            result={'tables':repo.snapshot(con),'product_types':list(con.execute('SELECT product_type,COUNT(*) FROM affiliate_products GROUP BY 1'))}
        print(json.dumps(result,ensure_ascii=False,indent=2))
