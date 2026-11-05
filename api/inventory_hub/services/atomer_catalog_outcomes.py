"""Distinguish acknowledged new products from existing identities and uncertain POSTs."""
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import text

from inventory_hub.services.product_link_source import _navigation


def capture(job, observed):
    from inventory_hub.services.atomer_new_native import _same_text

    doc=job['document']; receipt=job['receipt']; old=receipt.get('native_origin')
    unconfirmed={**{k:v for k,v in old.items() if k!='sample'},'current_identity':False} if old else None
    if not observed.get('identity_verified') or not observed.get('product_id'):
        return unconfirmed
    root=doc['payload']; live=observed.get('native') or {}; pid=observed['product_id']
    if live.get('code')!=root['code'] or live.get('product_id')!=pid:
        return unconfirmed
    if old and old.get('product_id')!=pid:
        return unconfirmed
    if not old:
        if not receipt.get('dispatched'):
            return None
        if doc.get('extension') or doc.get('extension_plan'):
            kind='existing' if (doc.get('extension') or {}).get('existing_report_run') else 'unconfirmed_origin'
        else:
            ack=receipt.get('ack') or {}
            inserted=ack.get('inserted') is True and ack.get('product_id') in (None,pid)
            kind='created' if inserted else 'unconfirmed_origin'
        old={'kind':kind,'product_id':pid,
             'code':live['code'],'observed_at':datetime.now(timezone.utc).isoformat()}
    old={**old,'current_identity':True}
    if old['kind']!='created':
        return old
    want=[d for d in root.get('descriptions',[]) if d.get('language')=='sk']
    actual=[d for d in live.get('descriptions',[]) if d.get('language')=='sk']
    leaves=live.get('variants') or []
    if (root.get('active_yn') is not True or live.get('active_yn') is not True or
            leaves and not any(v.get('active_yn') is True for v in leaves) or
            len(want)!=1 or len(actual)!=1 or actual[0].get('active_yn') is not True):
        return {k:v for k,v in old.items() if k!='sample'}
    fields=('short_description','long_description')
    formatted=any(want[0].get(k) for k in fields) and all(
        _same_text(actual[0].get(k),want[0].get(k),html=True) for k in fields)
    href=_navigation(live,live)['shop_url']
    url=urlsplit(href) if href else None
    # Only the observed public shop location can be shared as a storefront sample.
    if not formatted or not url or url.scheme!='https' or url.hostname not in ('xtrek.site','www.xtrek.site'):
        return {k:v for k,v in old.items() if k!='sample'}
    return {**old,'sample':dict(shop_url=href,title=actual[0].get('title') or live['code'],
                                html_verified=True,variant_count=len(leaves))}


async def summary(db,campaign,native_families):
    params=dict(c=campaign['id'],r=campaign['checked_run_id'])
    row=(await db.execute(text("""WITH bound AS (
        SELECT DISTINCT family_key FROM atomer_progress_items WHERE run_id=:r
    ), older AS (
        SELECT DISTINCT p.family_key FROM atomer_catalog_proofs p JOIN atomer_progress_items i
          ON i.run_id=:r AND i.family_key=p.family_key AND i.code=p.code
        WHERE p.campaign=:c AND p.evidence->>'status'='verified' AND p.evidence->>'scope' IS DISTINCT FROM 'biketrek'
          AND p.evidence->'xtrek'->>'product_id'=i.detail->>'product_id'
    ), families AS (
        SELECT j.family_key,j.receipt->'native_origin' AS origin,
            CASE WHEN j.receipt->'native_origin'->>'current_identity' IS DISTINCT FROM 'false'
                      AND j.receipt->'native_origin'->>'kind' IN ('created','existing') THEN j.receipt->'native_origin'->>'kind'
                 WHEN old.family_key IS NOT NULL AND j.receipt->>'dispatched' IS DISTINCT FROM 'true' THEN 'existing' END AS kind
        FROM atomer_catalog_jobs j JOIN bound USING(family_key) LEFT JOIN older old USING(family_key) WHERE j.campaign=:c
    ) SELECT count(*) FILTER(WHERE kind='created') AS created,count(*) FILTER(WHERE kind='existing') AS older,
        (SELECT jsonb_agg(sample ORDER BY first_seen,family_key) FROM (
            SELECT family_key,origin->>'observed_at' AS first_seen,origin->'sample' || jsonb_build_object(
                'family_key',family_key,'code',origin->>'code','product_id',origin->'product_id','observed_at',origin->>'observed_at') AS sample
            FROM families WHERE kind='created' AND jsonb_typeof(origin->'sample')='object'
            ORDER BY origin->>'observed_at',family_key LIMIT 5
        ) recent) AS examples FROM families"""),params)).mappings().one()
    return (dict(xtrek_created_families=row['created'],xtrek_existing_families=row['older'],
                 xtrek_origin_unknown=max(0,native_families-row['created']-row['older'])),row['examples'] or [])
