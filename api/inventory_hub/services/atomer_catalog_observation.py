"""Refresh a trusted Hub product snapshot with native deltas and current variant parameters."""
from datetime import datetime, timedelta, timezone

from inventory_hub.services import migration_upgates_export as export
from inventory_hub.services.upgates import UpgatesClient


def indexed(rows, key):
    result = {}
    for row in rows:
        value = row.get(key) if isinstance(row,dict) else None
        if type(value) is not int or value<=0 or value in result:
            raise export.ExportError('upgates_export_duplicate_or_missing_identity')
        result[value] = row
    return result


def page_rows(client, path, filters):
    seen = 0
    totals = None
    for n in range(1,export.MAX_PAGES+1):
        data = export._read(client,path,{**filters,'page':n,'current_page_items':100})
        records = data.get('products')
        count, pages = export._integer(data,'number_of_items'), export._integer(data,'number_of_pages')
        if (not isinstance(records,list) or export._integer(data,'current_page')!=n
                or export._integer(data,'current_page_items')!=len(records) or pages>export.MAX_PAGES
                or totals is not None and totals!=(count,pages) or n<pages and not records):
            raise export.ExportError('upgates_export_invalid_pagination')
        totals = count,pages
        yield from records
        seen += len(records)
        if n>=pages:
            if seen!=count:
                raise export.ExportError('upgates_export_incomplete_count')
            return
    raise export.ExportError('upgates_export_invalid_pagination')


def total(client, path, key):
    data=export._read(client,path,{'page':1,'current_page_items':1})
    count=export._integer(data,'number_of_items')
    if (export._integer(data,'current_page')!=1 or export._integer(data,'current_page_items')!=min(1,count)
            or not isinstance(data.get(key),list) or len(data[key])!=min(1,count)):
        raise export.ExportError('upgates_export_invalid_pagination')
    return count


def recent_products(client, cache, started):
    if not isinstance(cache,dict) or not isinstance(cache.get('products'),list) or not cache['products']:
        return None
    try:
        pulled=datetime.fromisoformat(cache['pulled_at'])
        if pulled.tzinfo is None:
            pulled=pulled.replace(tzinfo=timezone.utc)
        if not timedelta(0)<=started-pulled<=timedelta(hours=12):
            return None
        previous=indexed(cache['products'],'product_id')
    except (KeyError,TypeError,ValueError):
        return None
    # The shared cache is written after the pull. Overlap the full collection window.
    since=min(pulled-timedelta(hours=2),started)
    delta=indexed(page_rows(client,'products',{'last_update_time_from':since.isoformat(timespec="seconds")}),'product_id')
    previous.update(delta)
    if len(previous)!=total(client,'products','products'):
        return None  # Tombstone or incomplete cache. Use an entire new catalog.
    return list(previous.values())


def selection_products(client, cache, codes):
    """Full identity index plus a fresh direct read of every explicitly selected parent."""
    roots=recent_products(client,cache,datetime.now(timezone.utc))
    if roots is None or not codes:
        return roots
    parents=indexed(roots,'product_id')
    wanted=set(codes)
    selected={pid for pid,p in parents.items() if any(v.get('code') in wanted for v in p.get('variants') or [p])}
    observed={}
    ids=sorted(selected)
    for i in range(0,len(ids),100):
        chunk=ids[i:i+100]
        current=indexed(page_rows(client,'products',{'product_ids':';'.join(map(str,chunk))}),'product_id')
        if set(current)!=set(chunk):
            raise export.ExportError('upgates_export_catalog_changed')
        observed.update(current)
    parents.update(observed)
    return list(parents.values())


def parameter_rows(client, products):
    parents = indexed(products,'product_id')
    expected = {}
    variant_parents = []
    for pid,parent in parents.items():
        leaves = parent.get('variants') or []
        if not isinstance(leaves,list) or bool(leaves)!=parent.get('variants_exists_yn'):
            raise export.ExportError('upgates_export_catalog_changed')
        if not leaves:
            continue
        variant_parents.append(pid)
        for vid,v in indexed(leaves,'variant_id').items():
            if vid in expected or v.get('product_id')!=pid or v.get('product_code')!=parent['code']:
                raise export.ExportError('upgates_export_catalog_changed')
            expected[vid]=(pid,v)
    if len(expected)!=total(client,'products/variants','variants'):
        raise export.ExportError('upgates_export_catalog_changed')
    for i in range(0,len(variant_parents),100):
        ids=variant_parents[i:i+100]
        params=indexed(page_rows(client,'products/parameters',{'product_ids':';'.join(map(str,ids))}),'product_id')
        if set(params)!=set(ids):
            raise export.ExportError('upgates_export_family_incomplete')
        for pid,p in params.items():
            parent=parents[pid]
            if p.get('code')!=parent['code'] or not isinstance(p.get('variants'),list):
                raise export.ExportError('upgates_export_catalog_changed')
            for vid,v in indexed(p['variants'],'variant_id').items():
                owner,leaf=expected.pop(vid,(None,None))
                if owner!=pid or v.get('code')!=leaf['code'] or not isinstance(v.get('parameters'),list):
                    raise export.ExportError('upgates_export_catalog_changed')
                yield dict(product_id=pid,variant_id=vid,code=v['code'],ean=leaf.get('ean'),parameters=v['parameters'])
    if expected:
        raise export.ExportError('upgates_export_family_incomplete')


def catalog(cache=None):
    from inventory_hub.services import atomer_progress as progress
    started=datetime.now(timezone.utc)
    client=UpgatesClient.from_shop('xtrek')
    try:
        parents=recent_products(client,cache,started)
        if parents is None:
            parents=list(page_rows(client,'products',{}))
        leaves=list(parameter_rows(client,parents))
        # The remote API does not offer a transaction across endpoints. A concurrent edit
        # during this observation invalidates it even when all collection counts still match.
        changed=list(page_rows(client,'products',{'last_update_time_from':(started-timedelta(seconds=1)).isoformat(timespec='seconds')}))
        if changed:
            raise export.ExportError('upgates_export_catalog_changed')
        return progress.flatten_catalog(parents,leaves)
    finally:
        client.session.close()
