"""Operator-only read model. Campaigns, approvals and reconciliation consume the immutable report."""
from sqlalchemy import text

from inventory_hub.services import atomer_progress_store as snapshot, atomer_review as review
from inventory_hub.services.product_link_source import _url


# Every overlay is scoped to the published source pair, subject and family. A pending
# intent, a previous successful evidence nested in a blocked receipt, or an unlisted
# current shop link cannot establish a present-day binding.
FACTS = """
WITH observed_families AS MATERIALIZED (
 SELECT family_key,document#>>'{payload,code}' AS parent,receipt->>'product_id' AS pid,
        receipt->'variants' AS variants,receipt->'navigation' AS navigation
 FROM atomer_catalog_jobs WHERE campaign=:campaign AND receipt->>'identity_verified'='true'
   AND receipt->>'product_id' ~ '^[1-9][0-9]{0,17}$'
   AND stage IN ('cards','related','fidelity','updating','verified','partial')
), candidates AS (
 SELECT i.code,i.kind,i.family_key,i.state AS previous,i.detail AS d,
        l.checked_at AS local_at,lp.id AS local_id,lp.sku AS local_sku,
        p.state AS proof_state,p.checked_at AS proof_at,p.evidence AS proof,
        pp.id AS proof_id,pp.sku AS proof_sku,bp.id AS base_id,bp.sku AS base_sku,
        o.pid AS observed_pid,o.variants->>i.code AS observed_vid,o.parent AS observed_parent,
        o.navigation->i.code AS observed_navigation
 FROM atomer_progress_items i
 LEFT JOIN observed_families o ON o.family_key=i.family_key AND (
    (i.kind='variant' AND o.variants->>i.code ~ '^[1-9][0-9]{0,17}$' AND o.parent=i.detail->>'parent')
    OR (i.kind='standalone' AND o.parent=i.code AND o.variants='{}'::jsonb))
 LEFT JOIN atomer_catalog_source_cards l ON l.campaign=:campaign AND l.family_key=i.family_key
     AND l.code=i.code AND l.state='card_ready' AND l.evidence->>'status'='card_ready'
     AND l.evidence->>'card_id'=l.card_id::text
     AND coalesce(l.evidence->>'source_ean','')=coalesce(i.detail->>'ean','')
     AND EXISTS (SELECT 1 FROM atomer_progress_runs r WHERE r.id=l.run_id
                 AND r.source_sha256=:source AND r.admin_export_sha256=:admin)
 LEFT JOIN products lp ON lp.id=l.card_id AND lp.sku=l.evidence->>'hub_sku'
 LEFT JOIN atomer_catalog_proofs p ON p.campaign=:campaign AND p.family_key=i.family_key
     AND p.code=i.code AND p.state IN ('verified','partial') AND p.evidence->>'status'='verified'
     AND coalesce(p.evidence->>'ean','')=coalesce(i.detail->>'ean','')
 LEFT JOIN products pp ON pp.id=CASE WHEN p.evidence->>'card_id' ~ '^[1-9][0-9]{0,17}$'
                                    THEN (p.evidence->>'card_id')::bigint END
     AND pp.sku=p.evidence->>'hub_sku'
 LEFT JOIN products bp ON bp.id=CASE WHEN i.detail->>'hub_id' ~ '^[1-9][0-9]{0,17}$'
                                    THEN (i.detail->>'hub_id')::bigint END AND bp.sku=i.detail->>'hub_sku'
 WHERE i.run_id=:run
), cards AS (
 SELECT c.*,coalesce(proof_id,local_id,base_id) AS cid,coalesce(proof_sku,local_sku,base_sku) AS sku,
   (local_id IS NOT NULL AND proof_id IS NOT NULL AND local_id<>proof_id OR
    base_id IS NOT NULL AND coalesce(proof_id,local_id,base_id)<>base_id) AS conflict
 FROM candidates c
), matches AS (
 SELECT c.*,x.id AS x_link,b.id AS b_link,x.parent_code AS x_parent,b.parent_code AS b_parent,
        x.external_product_id AS x_pid,x.external_variant_id AS x_vid,x.external_code AS x_code,
        b.external_product_id AS b_pid,b.external_variant_id AS b_vid,b.external_code AS b_code,
        (proof_id IS NOT NULL AND x.id IS NOT NULL AND x.external_product_id=proof->'xtrek'->>'product_id'
         AND x.external_variant_id IS NOT DISTINCT FROM proof->'xtrek'->>'variant_id'
         AND x.external_code=proof->'xtrek'->>'code' AND x.is_variant=(x.external_variant_id IS NOT NULL)
         AND (NOT (proof->'xtrek' ? 'parent') OR x.parent_code IS NOT DISTINCT FROM proof->'xtrek'->>'parent')
         AND (proof->'xtrek'->>'link_id' IS NULL OR x.id::text=proof->'xtrek'->>'link_id')) AS new_x,
        (proof_id IS NOT NULL AND b.id IS NOT NULL AND b.external_product_id=proof->'biketrek'->>'product_id'
         AND b.external_variant_id IS NOT DISTINCT FROM proof->'biketrek'->>'variant_id'
         AND b.external_code=proof->'biketrek'->>'code' AND b.is_variant=(b.external_variant_id IS NOT NULL)
         AND (NOT (proof->'biketrek' ? 'parent') OR b.parent_code IS NOT DISTINCT FROM proof->'biketrek'->>'parent')
         AND (proof->'biketrek'->>'link_id' IS NULL OR b.id::text=proof->'biketrek'->>'link_id')) AS new_b,
        (x.id IS NOT NULL AND x.external_product_id=d->>'product_id' AND x.external_code=d->>'target_code'
         AND x.external_variant_id IS NOT DISTINCT FROM d->>'variant_id'
         AND x.parent_code IS NOT DISTINCT FROM nullif(d->>'target_parent','')) AS same_x,
        (b.id IS NOT NULL AND b.external_product_id=d->'bike'->>'product_id' AND b.external_code=d->'bike'->>'code'
         AND b.external_variant_id IS NOT DISTINCT FROM d->'bike'->>'variant_id'
         AND b.parent_code IS NOT DISTINCT FROM nullif(d->'bike'->>'parent','')) AS same_b
 FROM cards c
 LEFT JOIN shop_products x ON x.shop_id=(SELECT id FROM shops WHERE code='xtrek') AND x.product_id=c.cid AND x.is_listed
 LEFT JOIN shop_products b ON b.shop_id=(SELECT id FROM shops WHERE code='biketrek') AND b.product_id=c.cid AND b.is_listed
), effective AS (
 SELECT m.*,
    CASE WHEN conflict OR d->>'card_state'='review' THEN 'review'
         WHEN cid IS NOT NULL THEN 'exists' ELSE 'missing' END AS card_state,
    CASE WHEN previous='review' OR conflict THEN 'review'
         WHEN new_x OR (previous='linked' AND same_x) THEN 'linked'
         WHEN x_link IS NOT NULL AND NOT coalesce(same_x,false) THEN 'review'
         WHEN observed_pid IS NOT NULL THEN CASE WHEN cid IS NULL THEN 'needs_card' ELSE 'needs_link' END
         WHEN previous IN ('linked','needs_link','needs_card') THEN CASE WHEN cid IS NULL THEN 'needs_card' ELSE 'needs_link' END
         WHEN cid IS NOT NULL THEN 'needs_target' ELSE 'needs_both' END AS state,
    CASE WHEN conflict OR d->>'bike_state'='review' THEN 'review'
         WHEN cid IS NULL THEN 'no_card' WHEN new_b OR (d->>'bike_state'='linked' AND same_b) THEN 'linked'
         WHEN b_link IS NOT NULL THEN 'review' ELSE 'unlinked' END AS bike_state
 FROM matches m
)
"""


def _navigation(proof, old, same):
    return {k:_url(proof.get(k)) or (_url((old or {}).get(k)) if same else None)
            for k in ('shop_url','shop_admin_url')}


def _detail(row):
    d = dict(row['d'])
    p = row['proof'] or {}
    if row['conflict']:
        d.update(card_state='review',state='review',bike_state='review',hub_id=None,hub_sku=None,
                 candidates=sorted({i for i in (row['base_id'],row['local_id'],row['proof_id']) if i}),
                 reasons=sorted(set(d.get('reasons',[]))|{'card_identity_conflict'}))
        return d
    d.update(state=row['state'],card_state=row['card_state'],bike_state=row['bike_state'],
             hub_id=row['cid'],hub_sku=row['sku'])
    if row['state']=='review' and row['previous']!='review':
        d['reasons']=sorted(set(d.get('reasons',[]))|{'link_identity_differs'})
    if row['new_x']:
        xt=p['xtrek']; d.update(**_navigation(xt,d,row['same_x']),product_id=row['x_pid'],variant_id=row['x_vid'],
                                 target_code=row['x_code'],target_parent=row['x_parent'])
        if not row['same_x']:
            d['target_ean']=xt.get('ean')
    elif row['observed_pid'] is not None and row['state'] in ('needs_card','needs_link'):
        same=(d.get('product_id'),d.get('variant_id'),d.get('target_code'))==(
                row['observed_pid'],row['observed_vid'],d['code'])
        d.update(**_navigation(row['observed_navigation'] or {},d,same),
                 product_id=row['observed_pid'],variant_id=row['observed_vid'],target_code=d['code'],
                 target_parent=row['observed_parent'] if row['observed_vid'] else None)
        if not same:
            d['target_ean']=None
    elif row['previous']=='linked' and not row['same_x']:
        d['shop_url']=d['shop_admin_url']=None
    if row['new_b']:
        b=p['biketrek'];d['bike']=dict(product_id=row['b_pid'],variant_id=row['b_vid'],code=row['b_code'],
                        parent=row['b_parent'],link_id=row['b_link'],is_variant=row['b_vid'] is not None,
                        **_navigation(b,d.get('bike'),row['same_b']))
    elif row['bike_state'] in ('unlinked','no_card'):
        d['bike']=None
    elif row['bike_state']=='review' and row['b_link'] and not row['same_b']:
        d['bike']={'reason':'changed_identity'}
    if row['new_x'] or row['new_b']:
        d['catalog_status']='content_pending' if row['proof_state']=='partial' else 'links_verified'
    elif row['local_id'] is not None or row['proof_id'] is not None:
        d['catalog_status']='card_ready'
    return d


async def page(db,kind,state,bike,card,search,offset,limit,work='all'):
    head=await snapshot.latest(db)
    campaign=None
    if head and head['admin_export_sha256']:
        campaign=await db.scalar(text("""SELECT id FROM atomer_catalog_campaigns
          WHERE checked_run_id=:run AND source_sha=:source AND admin_sha=:admin
            AND state NOT IN ('source_changed') ORDER BY updated_at DESC,id LIMIT 1"""),
            dict(run=head['id'],source=head['source_sha256'],admin=head['admin_export_sha256']))
    if not campaign:
        return await snapshot.page(db,kind,state,bike,card,search,offset,limit,work)
    params=dict(run=head['id'],source=head['source_sha256'],admin=head['admin_export_sha256'],campaign=campaign,
                offset=offset,limit=limit)
    filters=[]
    for name,value in [('kind',kind),('state',state),('bike_state',bike),('card_state',card)]:
        if value:
            filters.append(f'e.{name}=:{name}');params[name]=value
    if search:
        filters.append("""strpos(lower(concat_ws(' ',e.code,d->>'name',d->>'ean',d->>'parent',
          CASE WHEN NOT conflict THEN e.cid END,CASE WHEN NOT conflict THEN e.sku END,
          CASE WHEN new_x THEN x_code ELSE d->>'target_code' END,
          CASE WHEN new_x THEN x_pid WHEN observed_pid IS NOT NULL AND state IN ('needs_card','needs_link') THEN observed_pid ELSE d->>'product_id' END,
          CASE WHEN new_x THEN x_vid WHEN observed_pid IS NOT NULL AND state IN ('needs_card','needs_link') THEN observed_vid ELSE d->>'variant_id' END,
          CASE WHEN new_b THEN b_code WHEN bike_state IN ('review','linked') AND (b_link IS NULL OR same_b) THEN d->'bike'->>'code' END,
          CASE WHEN new_b THEN b_pid WHEN bike_state IN ('review','linked') AND (b_link IS NULL OR same_b) THEN d->'bike'->>'product_id' END,
          CASE WHEN new_b THEN b_vid WHEN bike_state IN ('review','linked') AND (b_link IS NULL OR same_b) THEN d->'bike'->>'variant_id' END)),lower(:search))>0""");params['search']=search
    if work!='all':
        filters.append("""(EXISTS (SELECT 1 FROM atomer_review_work w WHERE w.status='done' AND
          ((w.kind='family' AND w.key=e.family_key) OR (w.kind='item' AND w.key=e.code))))=:closed""")
        params['closed']=work=='done'
    where=' AND '.join(filters) or 'true'
    sql=FACTS+f""", matched AS MATERIALIZED (SELECT e.* FROM effective e WHERE {where}),
       families AS (SELECT family_key,min(d->>'family_name') AS name,count(*) AS size FROM matched GROUP BY family_key),
       chosen AS (SELECT * FROM families ORDER BY lower(name),family_key LIMIT :limit OFFSET :offset),
       totals AS (SELECT coalesce(sum(size),0)::bigint AS total,count(*) AS families FROM families)
       SELECT t.total,t.families,statement_timestamp() AS read_at,g.name,m.* FROM totals t
       LEFT JOIN chosen g ON true LEFT JOIN matched m ON m.family_key=g.family_key
       ORDER BY lower(g.name),g.family_key,m.code"""
    rows=(await db.execute(text(sql),params)).mappings().all()
    groups={}
    for row in rows:
        key=row['family_key']
        if key is not None:
            group=groups.setdefault(key,dict(key=key,name=row['name'],items=[]))
            group['items'].append(_detail(row))
    if groups:
        extras=await db.execute(text('''SELECT family_key,count(*) FROM atomer_review_checks WHERE run_id=:run
          AND family_key=ANY(CAST(:keys AS text[])) GROUP BY family_key'''),dict(run=head['id'],keys=list(groups)))
        for key,n in extras:groups[key]['extra_count']=n
        await review.decorate(db,groups)
    first=rows[0]
    return dict(run=dict(head),total=first['total'],families=first['families'],groups=list(groups.values()),
                projection={'read_at':first['read_at']})
