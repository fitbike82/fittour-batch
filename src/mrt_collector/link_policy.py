"""Shared affiliate URL extraction/reuse policy, without network access."""
import urllib.parse


def extract(body):
    if isinstance(body,str):
        url=urllib.parse.urlparse(body)
        if url.scheme=='https' and url.hostname=='myrealt.rip' and url.path not in ('','/') and not url.username and not url.password:
            return body
    elif isinstance(body,dict):
        for value in body.values():
            found=extract(value)
            if found:return found
    elif isinstance(body,list):
        for value in body:
            found=extract(value)
            if found:return found
    return None


def existing(con,product_id,target_url,placement='default'):
    # Target must match: the same product ID can later acquire a different URL.
    candidates=con.execute('''SELECT affiliate_url FROM affiliate_links
        WHERE affiliate_company_id=1 AND target_url=?
        AND (affiliate_product_id=? OR affiliate_product_id IS NULL)
        ORDER BY CASE WHEN placement=? THEN 0 ELSE 1 END,id''',(target_url,product_id,placement))
    return next(((row[0],) for row in candidates if extract(row[0])),None)
