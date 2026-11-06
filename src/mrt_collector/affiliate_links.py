"""Verify one unlinked product per type; reuse existing links first.

Only --confirm makes remote MyLink requests. No search/detail collection.
"""
import argparse
import json
from types import SimpleNamespace
from . import enrichment as e


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm',action='store_true')
    args=parser.parse_args()
    if not args.confirm:
        parser.error('실제 제휴 링크 발급에는 --confirm이 필요합니다.')
    e.load_env();con=e.get_db();e.setup(con)
    report={'verified_at':e.now(),'reused':[],'samples':[]}
    opts=SimpleNamespace(placement='default')
    for product in e.rows(con):
        if e.existing_link(con,product[0],product[4],opts.placement):
            con.execute("INSERT OR IGNORE INTO enrichment_runs(product_id,kind,status,updated_at) VALUES(?,'link','PENDING',?)",(product[0],e.now()));con.commit()
            e.act(con,'link',product,None,opts)
            report['reused'].append(product[0])
    for typ in ('tna','accommodation'):
        product=next((p for p in e.rows(con,typ) if p[4] and not e.existing_link(con,p[0],p[4],opts.placement)),None)
        if product is None:
            report['samples'].append({'type':typ,'status':'NO_UNLINKED_PRODUCT'});continue
        con.execute("INSERT OR IGNORE INTO enrichment_runs(product_id,kind,status,updated_at) VALUES(?,'link','PENDING',?)",(product[0],e.now()));con.commit()
        try:
            status=e.act(con,'link',product,None,opts)
            row=con.execute("SELECT error,response_path FROM enrichment_runs WHERE product_id=? AND kind='link'",(product[0],)).fetchone()
            report['samples'].append({'type':typ,'db_id':product[0],'status':status,'error':row[0],'response_path':row[1],'link_saved':bool(e.existing_link(con,product[0],product[4],opts.placement))})
        except Exception as exc:
            report['samples'].append({'type':typ,'db_id':product[0],'status':'TRANSPORT_ERROR','error':str(exc)})
    path=e.OUT/'link_verification.json';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if any(x['status']!='SUCCESS' for x in report['samples']):
        raise SystemExit(1)


if __name__=='__main__':main()
