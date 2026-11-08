"""Complete operation-local linked inputs; no cross-product family selection."""
from inventory_hub.services import product_dependency_scope as s


def by_id(rows):
    return {row['document']['id']: row['document'] for row in rows}


async def drafts(closure, identifiers):
    rows = await closure.rows('linked_drafts', 'product_import_drafts',
        't.id=ANY(CAST(:ids AS text[]))', {'ids': sorted(identifiers)})
    return by_id(rows)


async def catalog(closure, identifiers):
    rows = await closure.rows('catalog_products', 'supplier_products',
        't.id=ANY(CAST(:ids AS bigint[]))', {'ids': sorted(identifiers)})
    feed_ids = {row['document']['source_feed_id'] for row in rows if row['document'].get('source_feed_id')}
    feeds = by_id(await closure.rows('catalog_feeds', 'supplier_feeds',
        't.id=ANY(CAST(:ids AS bigint[]))', {'ids': sorted(feed_ids)}))
    owners = by_id(await closure.rows('catalog_suppliers', 'suppliers',
        't.id=ANY(CAST(:ids AS bigint[]))', {'ids': sorted({row['supplier_id'] for row in feeds.values()})}))
    result = {}
    for row in rows:
        raw = row['document']
        data = (raw.get('attributes') or {}).get('catalog')
        feed = feeds.get(raw.get('source_feed_id'))
        if isinstance(data, dict) and raw.get('is_active') is True and feed is not None:
            supplier = owners.get(feed['supplier_id'], {}).get('code')
            if (supplier is None or raw['supplier_id'] != feed['supplier_id']
                    or data.get('supplier') != supplier or data.get('feed_key', 'products') != feed['code']):
                raise s.Unverified('catalog_source_namespace_changed', '/supplier_products/' + str(raw['id']))
            result[(supplier, feed['code'], raw['id'])] = {**data, 'id': raw['id'], 'listed': False}
    return result


async def ai(closure, raw):
    context = s.obj(raw.get('context'), path='/context')
    linked, products = {}, {}
    staging = context.get('staging_id')
    if staging:
        if not isinstance(staging, str):
            raise s.Unverified('ai_draft_link_missing', '/context/staging_id')
        linked = await drafts(closure, {staging})
    elif context.get('source_kind', 'catalog') == 'catalog':
        identifiers = {value for value in context.get('product_ids', [])
                       if type(value) is int and value > 0} if isinstance(context.get('product_ids'), list) else set()
        products = await catalog(closure, identifiers)
    return linked, products


async def atomer(closure, raw):
    """One exact (campaign,family_key) closure, including every source sibling."""
    campaigns = by_id(await closure.rows('atomer_campaign', 'atomer_catalog_campaigns',
        't.id=:campaign', {'campaign': raw['campaign']}, header_projection='atomer-campaign-v1'))
    campaign = campaigns.get(raw['campaign'])
    if campaign is None:
        return None, None, {}, None
    heads = await closure.rows('atomer_current_run', 'atomer_progress_runs',
        't.id=(SELECT max(id) FROM atomer_progress_runs)', {}, header_projection='atomer-current-run-v1')
    head = heads[0]['document'] if len(heads) == 1 else None
    # Binding the pair through this exact campaign's checked_run_id avoids an
    # independent campaign-IN/family-IN cross product and the former global join.
    source_rows = await closure.rows('atomer_family', 'atomer_progress_items',
        't.run_id=:run_id AND t.family_key=:family',
        {'run_id': campaign.get('checked_run_id'), 'family': raw['family_key']})
    codes = [row['document']['code'] for row in source_rows]
    work_rows = await closure.rows('atomer_item_work', 'atomer_review_work',
        "t.kind='item' AND t.key=ANY(CAST(:codes AS text[]))", {'codes': sorted(codes)})
    work = {row['document']['key']: row['document'] for row in work_rows}
    group_rows = await closure.rows('atomer_family_work', 'atomer_review_work',
        "t.kind='family' AND t.key=:family", {'family': raw['family_key']})
    group = group_rows[0]['document'] if group_rows else {}
    family = {}
    for row in source_rows:
        item = row['document']
        current = work.get(item['code'], {})
        family[item['code']] = {'detail': item['detail'], 'revision': current.get('revision'),
            'status': current.get('status'), 'scope': current.get('scope'),
            'family_guard': [group.get('revision') or 0, group.get('status') or 'open', group.get('scope') or 'default']}
    identifiers = set()
    for card in ((raw.get('receipt') or {}).get('cards') or {}).values():
        for namespace in ('biketrek', 'xtrek'):
            leaf = (card.get('evidence') or {}).get(namespace)
            if isinstance(leaf, dict) and type(leaf.get('link_id')) is int and leaf['link_id'] > 0:
                identifiers.add(leaf['link_id'])
    maps = await closure.rows('atomer_mapping_claims', 'shop_products',
        't.id=ANY(CAST(:ids AS bigint[]))', {'ids': sorted(identifiers)})
    shops = by_id(await closure.rows('atomer_claim_shops', 'shops',
        't.id=ANY(CAST(:ids AS bigint[]))', {'ids': sorted({row['document']['shop_id'] for row in maps})}))
    claims = {row['document']['id']: {**row['document'], 'shop_code': shops.get(row['document']['shop_id'], {}).get('code')}
              for row in maps}
    return family, campaign, claims, head
