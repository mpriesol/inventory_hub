"""Bounded xTrek creation and independent native observation for journaled Atomer families.

The caller pins the source, owns the operator revisions and records intent before
dispatch. A POST is never replayed here, including when the acknowledgement is lost.
"""
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from time import monotonic

import requests

from inventory_hub.adapters.atomer_content import AtomerSourceError, html_fingerprint
from inventory_hub.services import atomer_family_batch as batch
from inventory_hub.services.migration_upgates_export import ExportError
from inventory_hub.services import stock_publication_source as transport
from inventory_hub.services.atomer_generations import image_name, same_image
from inventory_hub.services.atomer_review_targets import ReviewError
from inventory_hub.services.product_identity import barcode_keys
from inventory_hub.services.product_link_source import _navigation
from inventory_hub.services.product_publication_source import plain
from inventory_hub.services.sku_policy import brand_key
from inventory_hub.services.upgates import _retry_after_seconds

MAX_BATCH = 100
MAX_REQUEST_BYTES = 8_000_000
OTHER_RELATIONS = ('accessories', 'alternative', 'gifts', 'sets')


def _check(condition, code='new_native_invalid'):
    if not condition:
        raise ReviewError(code)


def _numeric(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _positive(value):
    return type(value) is int and value > 0


def _entries(documents, *, writing=False):
    _check(isinstance(documents, list) and 0 < len(documents) <= MAX_BATCH, 'new_batch_limit')
    result, identities = {}, set()
    count = 0
    for doc in documents:
        parent = doc['payload']
        children = parent.get('variants', [])
        _check(isinstance(children, list) and len(children) <= 2500, 'new_batch_limit')
        count += max(1, len(children))
        _check(count <= (MAX_BATCH if writing else 5000), 'new_batch_limit')
        for row in [parent, *children]:
            code = transport._code(row.get('code'))
            _check(code.casefold() not in identities, 'new_batch_duplicate_code')
            identities.add(code.casefold())
        result[parent['code']] = doc
    return result


def _by_id(rows, key):
    result = {}
    for row in rows:
        _check(isinstance(row, dict) and _positive(row.get(key)) and row[key] not in result,
               'new_native_identity_invalid')
        result[row[key]] = row
    return result


def _pages(client, path, key, filters):
    return batch.collect_pages(client, path, key, filters)


def _sparse(client, values, path='products', key='products', field='codes', filters=None):
    # Keep query strings within proxy limits, including multi-byte or percent-escaped SKUs.
    from urllib.parse import quote
    rows, part, length = [], [], 0
    for value in sorted(set(values)):
        encoded = len(quote(str(value), safe=''))+3
        if part and (len(part)>=MAX_BATCH or length+encoded>6000):
            rows.extend(_pages(client, path, key, {**(filters or {}),field:';'.join(part)}))
            part, length = [], 0
        part.append(str(value))
        length += encoded
    if part:
        rows.extend(_pages(client, path, key, {**(filters or {}),field:';'.join(part)}))
    return rows


def _tokens(row):
    yield ('code',row['code'].casefold())
    for ean in barcode_keys([row.get('ean')]):
        yield ('ean',ean)


def preflight(documents, fingerprint, checked_at):
    """The pinned full catalog run supplies baseline absence; check current namespace and all changed GTINs."""
    selected = _entries(documents)
    leaves={code:[doc['payload'],*(doc['payload'].get('variants') or [])] for code,doc in selected.items()}
    for code, doc in selected.items():
        # A disputed GTIN is never a search/creation key. Its exact SKU still
        # fences an apparently new family if a legacy native sibling owns it.
        deferred = doc.get('deferred_leaves') or {}
        _check(isinstance(deferred, dict) and len(deferred) <= 2500, 'new_deferred_leaf_invalid')
        for sku in deferred:
            leaves[code].append({'code': transport._code(sku)})
    return _check_absent(leaves,fingerprint,checked_at)


def _delta(client, fingerprint, checked_at, interests):
    from inventory_hub.routers.upgates_sync import _cache_path
    from inventory_hub.services import atomer_create_delta
    try:
        return atomer_create_delta.candidates(client, fingerprint, checked_at, interests, _cache_path('xtrek').parent)
    except ExportError as exc:
        if str(exc) in ('upgates_export_budget_deferred', 'upgates_export_http_429'):
            raise transport.SourceError('stock_publication_rate_limited', 429, 60, request_sent=False) from None
        raise transport.SourceError('new_native_changes_unavailable', request_sent=False) from None


def _check_absent(leaves, fingerprint, checked_at):
    _check(isinstance(checked_at,datetime) and checked_at.utcoffset() is not None,'new_run_time_missing')
    by_token = defaultdict(list)
    problems = {code:[] for code in leaves}
    for parent,new in leaves.items():
        for leaf in new:
            for token in _tokens(leaf):
                by_token[token].append(parent)
    for (kind, key), owners in by_token.items():
        if len(owners)>1:
            for owner in set(owners):
                problems[owner].append(f'{kind} {key}: viackrát v dávke')
    codes = [r['code'] for new in leaves.values() for r in new]
    with _connection(fingerprint) as client:
        positive = _delta(client, fingerprint, checked_at, by_token)
        selected_products=_sparse(client,codes,'products/simple')
        variants=_sparse(client,codes,'products/variants','variants','variant_codes')
        # A delta may retain a later-deleted parent's old identifiers; verify
        # its current IDs before treating an identity as occupied.
        direct=_sparse(client,positive,'products/simple',field='product_ids') if positive else []
        _check(all(p.get('product_id') in positive for p in direct),'new_native_filter_unconfirmed')
    folded = {s.casefold() for s in codes}
    _check(all(isinstance(p.get('code'),str) and p['code'].casefold() in folded for p in selected_products)
           and all(isinstance(v.get('code'),str) and v['code'].casefold() in folded for v in variants),
           'new_native_filter_unconfirmed')
    for parent in direct+selected_products:
        _check(_positive(parent.get('product_id')) and isinstance(parent.get('variants'),list)
               and bool(parent['variants']) is parent.get('variants_exists_yn'),'new_native_identity_invalid')
        for leaf in [parent,*parent['variants']]:
            _mark_collisions(by_token,problems,leaf,parent['product_id'])
    for leaf in variants:
        _check(_positive(leaf.get('variant_id')) and _positive(leaf.get('product_id')),'new_native_identity_invalid')
        _mark_collisions(by_token,problems,leaf,leaf['product_id'])
    return {code:sorted(set(issues)) for code,issues in problems.items()}


def _mark_collisions(owners,problems,leaf,pid):
    _check(isinstance(leaf.get('code'),str),'new_native_identity_invalid')
    native_tokens = set(_tokens(leaf))
    native_tokens.update(('ean', gtin) for gtin in barcode_keys([leaf['code']]))
    for token in native_tokens:
        for root in owners.get(token,[]):
            vid=leaf.get('variant_id')
            target=f'{pid}/{vid}' if vid else str(pid)
            problems[root].append(f'{token[0]} {token[1]}: xTrek {target} ({leaf["code"]})')


@contextmanager
def _connection(fingerprint):
    client = transport._client('xtrek',fingerprint)
    try:
        yield client
    finally:
        client.session.close()


@contextmanager
def _before_write(fingerprint):
    # A provider 429 on a GET is a sent read, but categorically no write.
    # Preserve that distinction for the journal's safe re-planning path.
    try:
        with _connection(fingerprint) as client:
            yield client
    except transport.SourceError as error:
        error.request_sent = False
        error.uncertain = False
        raise


def _post(client, payload, *, route='products'):
    _check(route in ('products', 'categories'), 'new_native_route_invalid')
    encoded = json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
    _check(len(encoded)<=MAX_REQUEST_BYTES,'new_native_payload_limit')
    # Use the same connection and budget. Redirections cannot move authorization or payload to another host.
    try:
        started=monotonic()
        with client.session.post(f'{client.base_url}/{route}',data=encoded,headers={'Content-Type':'application/json'},
                                 timeout=(5,30),verify=True,allow_redirects=False,stream=True) as response:
            if response.status_code==429:
                raise transport.SourceError('stock_publication_rate_limited',429,
                    _retry_after_seconds(response.headers.get('Retry-After')),
                    request_sent=getattr(response,'hub_request_sent',None) is not False)
            if response.status_code in (401,403):
                raise transport.SourceError('stock_publication_upgates_access',502,request_sent=True)
            if response.status_code not in (200,201):
                raise transport.SourceError('new_native_create_unconfirmed',uncertain=True,request_sent=True)
            raw=bytearray()
            for part in response.iter_content(chunk_size=65536):
                raw.extend(part)
                if len(raw)>2_000_000 or monotonic()-started>45:
                    raise transport.SourceError('new_native_create_unconfirmed',uncertain=True,request_sent=True)
            result=json.loads(raw,object_pairs_hook=transport._unique_object,parse_constant=transport._invalid_constant)
            if not isinstance(result,dict):
                raise ValueError()
            return result
    except (requests.RequestException,ValueError,RecursionError):
        raise transport.SourceError('new_native_create_unconfirmed',uncertain=True,request_sent=True) from None


def _messages(row):
    messages=row.get('messages') or []
    if not isinstance(messages,list):
        return ['Nečitateľné potvrdenie Upgates']
    return [str(m.get('message') or m.get('property') or 'Upgates')[:240]
            if isinstance(m,dict) else 'Nečitateľné potvrdenie Upgates'
            for m in messages if not isinstance(m,dict) or m.get('level')!='info']


def send(documents, fingerprint):
    """Dispatch one create-only request after durable intent; acknowledgements are hints, not proof."""
    selected = _entries(documents,writing=True)
    from inventory_hub.services.catalog_import import _assert_payload
    for doc in selected.values():
        _assert_payload(doc['payload'], expected_active=doc['payload']['active_yn'])
    payload={'products':[d['payload'] for d in selected.values()]}
    with _connection(fingerprint) as client:
        reply=_post(client,payload)
    result={code:dict(product_id=None,variant_ids={},issues=_messages(reply)) for code in selected}
    received=set()
    for row in reply.get('products') or []:
        if not isinstance(row,dict) or row.get('code') not in selected or row['code'] in received:
            raise transport.SourceError('new_native_create_unconfirmed',uncertain=True,request_sent=True)
        code=row['code']
        received.add(code)
        record=result[code]
        record['inserted'] = row.get('inserted_yn') is True
        record['issues'].extend(_messages(row))
        if row.get('inserted_yn') is not True:
            record['issues'].append(code + ' · Upgates nepotvrdil vytvorenie')
        if _positive(row.get('product_id')):
            record['product_id']=row['product_id']
        wanted={v['code'] for v in selected[code]['payload'].get('variants',[])}
        for v in row.get('variants') or []:
            if not isinstance(v,dict) or v.get('code') not in wanted:
                raise transport.SourceError('new_native_create_unconfirmed',uncertain=True,request_sent=True)
            record['issues'].extend(_messages(v))
            if v.get('inserted_yn') is not True:
                record['issues'].append(v['code']+' · Upgates nepotvrdil vytvorenie variantu')
            if _positive(v.get('variant_id')):
                record['variant_ids'][v['code']]=v['variant_id']
    for code in set(selected)-received:
        result[code]['issues'].append('Upgates nevrátil potvrdenie produktu')
    return result


def _sk(items, field):
    rows=[r.get(field) for r in items if isinstance(r,dict) and r.get('language')=='sk']
    return rows[0] if len(rows)==1 else None


def _parameters(rows):
    if not isinstance(rows,list):
        return None
    result={}
    for p in rows:
        name=_sk(p.get('descriptions') or [],'name')
        vals=tuple(sorted(str(_sk(v.get('descriptions') or [],'value') or '') for v in p.get('values') or []))
        if not name or not vals or '' in vals or name in result:
            return None
        result[name]=vals
    return result


def _same_text(actual,expected,html=False):
    if actual in ('',None) and expected in ('',None):
        return True
    if actual==expected:
        return True
    if html and isinstance(actual,str) and isinstance(expected,str):
        try:
            return html_fingerprint(actual)==html_fingerprint(expected)
        except AtomerSourceError:
            pass
    return False


def _supplements(client, parents, field):
    rows=_sparse(client,parents,'products/'+field,'products','product_ids')
    observed=_by_id(rows,'product_id')
    if field == 'related':
        # The complete, filtered related endpoint only returns products with
        # any related/accessory/alternative/gift/set entries (Upgates v2).
        _check(observed.keys() <= parents.keys(), 'new_native_filter_unconfirmed')
        for pid in parents.keys() - observed.keys():
            for key in ('related', *OTHER_RELATIONS):
                parents[pid][key] = []
    else:
        _check(set(observed)==set(parents),'new_native_supplement_incomplete')
    for pid,row in observed.items():
        parent=parents[pid]
        _check(row['code']==parent['code'],'new_native_identity_changed')
        if field=='related':
            _check(isinstance(row.get(field),list),'new_native_supplement_incomplete')
            for key in ('related', *OTHER_RELATIONS):
                values = row.get(key, [])
                _check(isinstance(values, list), 'new_native_supplement_incomplete')
                parent[key] = values
            continue
        data_fields=('parameters','parameters_new') if field=='parameters' else ('labels',)
        variants=_by_id(row.get('variants') or [],'variant_id')
        base=_by_id(parent.get('variants') or [],'variant_id')
        _check(base.keys()==variants.keys(),'new_native_supplement_incomplete')
        for core,extra in [(parent,row),*((base[k],variants[k]) for k in base)]:
            _check(core['code']==extra['code'],'new_native_identity_changed')
            for f in data_fields:
                _check(isinstance(extra.get(f),list),'new_native_supplement_incomplete')
                core[f]=extra[f]


def _read(client,selected,expected_ids):
    # IDs from acknowledgements are additional anchors, not replacements for the source code.
    by_id, by_code = {}, {}
    for code in selected:
        pid=(expected_ids.get(code) or {}).get('product_id')
        if pid is None:
            by_code[code]=None
        else:
            _check(_positive(pid) and pid not in by_id,'new_expected_id_invalid')
            by_id[pid]=code
    def bulk(anchors,field):
        large,ordinary=[],[]
        for key in anchors:
            code=by_id[key] if field=='product_ids' else key
            entry=selected[code]
            size=max(len(entry['payload'].get('variants') or []),len((entry.get('before') or {}).get('variants') or []))
            (large if size>=80 else ordinary).append(key)
        results=_sparse(client,ordinary,field=field) if ordinary else []
        if large:
            thin=_sparse(client,large,field=field,filters={'variants_yn':'false'})
            parents=_by_id(thin,'product_id')
            flats=_sparse(client,parents,'products/variants','variants','product_ids')
            children=defaultdict(list)
            for leaf in _by_id(flats,'variant_id').values():
                _check(leaf.get('product_id') in parents and leaf.get('product_code')==parents[leaf['product_id']]['code'],
                       'new_native_identity_changed')
                children[leaf['product_id']].append(leaf)
            for pid,head in parents.items():
                head['variants']=children[pid]
            results.extend(thin)
        return results
    rows=bulk(by_code,'codes')
    _check(all(r.get('code') in by_code for r in rows),'new_native_filter_unconfirmed')
    direct=bulk(by_id,'product_ids')
    _check(all(r.get('product_id') in by_id for r in direct),'new_native_filter_unconfirmed')
    parents=_by_id(rows+direct,'product_id')
    _check(len({r['code'] for r in parents.values()})==len(parents),'new_native_identity_invalid')
    # A renamed or reused ID is an identity conflict, and must not get silently looked up by its old SKU.
    for pid,p in parents.items():
        _check(isinstance(p.get('variants'),list) and bool(p['variants']) is p.get('variants_exists_yn'),
               'new_native_supplement_incomplete')
        for leaf in p['variants']:
            _check(leaf.get('product_id')==pid and leaf.get('product_code')==p['code'],
                   'new_native_identity_changed')
    if parents:
        for field in ('parameters','labels','related'):
            keys = ('parameters', 'parameters_new') if field == 'parameters' else (field,)
            sparse = parents if field == 'related' else {
                pid: parent for pid, parent in parents.items()
                if any(not isinstance(row.get(k), list) for row in (parent, *parent['variants']) for k in keys)
            }
            if sparse:
                _supplements(client, sparse, field)
    return {by_id.get(pid,p['code']):p for pid,p in parents.items()}


def _price_same(remote,expected,vat):
    rows=remote.get('prices') or []
    for planned in expected:
        matches=[g for g in rows if g.get('language')==planned['language'] and g.get('currency')=='EUR']
        if len(matches)!=1:
            return False
        current=matches[0]
        if _numeric(current.get('vat'))!=_numeric(vat) or _numeric(current.get('price_common'))!=_numeric(planned['price_common']):
            return False
        for wanted in planned['pricelists']:
            lists=[p for p in current.get('pricelists') or [] if p.get('name')==wanted['name']]
            if len(lists)!=1 or any(_numeric(lists[0].get(key))!=_numeric(wanted['price_original'])
                                      for key in ('price_original','price_with_vat')):
                return False
    return True


def _image_issues(remote,wanted,variant):
    actual=remote.get('images')
    if not isinstance(actual,list):
        return ['galéria sa nedá overiť']
    issues, positions=[],[]
    for planned in wanted:
        matched=[im for im in actual if same_image(im.get('url'),planned['url']) and _positive(im.get('file_id'))]
        caption=_sk(planned.get('titles') or [],'title')
        if len(matched)!=1:
            issues.append(image_name(planned['url']))
            continue
        img=matched[0]
        if (bool(img.get('main_yn'))!=bool(planned.get('main_yn')) or
                (not variant and bool(img.get('list_yn'))!=bool(planned.get('list_yn'))) or
                (caption and _sk(img.get('titles') or [],'title')!=caption)):
            issues.append(image_name(planned['url'])+' (hlavný obrázok alebo popis)')
        positions.append(img.get('position'))
    if any(type(p) is not int for p in positions) or positions!=sorted(positions) or len(set(positions))!=len(positions):
        issues.append('poradie obrázkov')
    return issues


def _labels_match(remote,expected,source_names,managed_ids):
    plan={x['label_id']:x['active_yn'] for x in expected}
    actual=remote.get('labels')
    if not isinstance(actual,list):
        return False
    enabled={x.get('label_id') for x in actual if x.get('active_currently_yn') is True}
    # A newly created product's effective flags must reflect the source, including inherited labels.
    if managed_ids is not None:
        enabled.intersection_update(managed_ids)
    return enabled=={lid for lid,on in plan.items() if on} and len(source_names)==sum(bool(x) for x in plan.values())


def _family_result(doc,native,expected):
    root=doc['payload']
    result=dict(status='missing',product_id=None,variants={},identity_verified=False,verified_fields=False,
                issues=[],deferred=[],native=None)
    if native is None:
        result['issues']=['produkt v natívnom čítaní chýba']
        return result
    result['product_id']=native['product_id']
    result['native']=plain(native)
    wanted={r['code']:r for r in root.get('variants') or []}
    actual={r['code']:r for r in native['variants']}
    result['variants']={s:v['variant_id'] for s,v in actual.items()}
    anchors=(expected or {}).get('variant_ids') or {}
    identity = (native['code']==root['code'] and len(actual)==len(native['variants']) and set(actual)==set(wanted)
        and not native.get('archived_yn') and not native.get('set_yn')
        and (not expected or not expected.get('product_id') or expected['product_id']==native['product_id'])
        and all(s in actual and actual[s]['variant_id']==vid for s,vid in anchors.items()))
    issues, deferred = result['issues'], result['deferred']
    if not identity:
        result.update(status='conflict',issues=['natívny kód, ID, skladba variantov alebo typ rodiča nesúhlasí'])
        return result
    for code,planned in [(root['code'],root),*wanted.items()]:
        live=actual[code] if code in wanted else native
        variant=code in wanted
        if (live.get('ean') or '')!=(planned.get('ean') or ''):
            identity=False
            issues.append(code+' · EAN')
        if variant and _parameters(live.get('parameters_new'))!=_parameters(planned.get('parameters',[])):
            identity=False
            issues.append(code+' · parametre variantu')
        if live.get('active_yn') is not planned['active_yn']:
            issues.append(code+' · viditeľnosť')
        if variant and bool(live.get('main_yn'))!=bool(planned.get('main_yn')):
            issues.append(code+' · predvolený variant')
        if _numeric(live.get('weight'))!=_numeric(planned.get('weight')):
            issues.append(code+' · hmotnosť')
        if not _price_same(live,planned['prices'],root['vats']['sk']):
            issues.append(code+' · cena alebo DPH')
        if 'availability_id' in planned and (live.get('availability_id')!=planned['availability_id'] or
            (live.get('availability') or '').casefold()!=doc['availability'][code].casefold()):
            issues.append(code+' · dostupnosť')
        if not _labels_match(live,planned.get('labels',[]),doc['flags'][code],doc.get('managed_label_ids')):
            issues.append(code+' · príznaky')
        missing=_image_issues(live,planned.get('images',[]),variant)
        if missing:
            deferred.append('images')
            issues.append(code+' · galéria: '+', '.join(missing))
    if brand_key(native.get('manufacturer') or '')!=brand_key(root.get('manufacturer') or ''):
        identity=False
        issues.append(root['code']+' · výrobca')
    if _numeric((native.get('vats') or {}).get('sk'))!=_numeric(root['vats']['sk']):
        issues.append('DPH rodiča')
    categories={(c.get('category_id'),c.get('main_yn')) for c in native.get('categories') or []}
    if categories!={(c['category_id'],c['main_yn']) for c in root['categories']}:
        issues.append('kategórie')
    attrs=_parameters(native.get('parameters_new'))
    if attrs!=_parameters(root.get('parameters',[])):
        issues.append('parametre produktu')
    for lang in root['descriptions']:
        rows=[r for r in native.get('descriptions') or [] if r.get('language')==lang['language']]
        if len(rows)!=1:
            issues.append('chýbajúci jazyk '+lang['language'])
            continue
        for field in ('title','short_description','long_description','seo_title','seo_description'):
            if not _same_text(rows[0].get(field),lang.get(field),field in ('short_description','long_description')):
                issues.append(field)
    related=set(native['related'])
    wanted_related=set(root.get('related',[]))
    if related-wanted_related:
        issues.append('neočakávané súvisiace produkty')
    if wanted_related-related:
        deferred.append('related')
        issues.append('chýbajú súvisiace produkty: '+', '.join(sorted(wanted_related-related)))
    issues.extend(str(issue)[:640] for issue in doc.get('issues') or [])
    if doc.get('missing_source_features'):
        deferred.append('missing_source_features')
        issues.extend(str(detail)[:640] for detail in doc['missing_source_features'])
    unresolved_categories = doc.get('missing_categories') or doc.get('pending_categories') or []
    if unresolved_categories:
        deferred.append('missing_categories')
        issues.append('chýbajúce ďalšie kategórie: '+', '.join(unresolved_categories))
    if doc.get('missing_related'):
        deferred.append('missing_related')
        issues.append('súvisiace produkty čakajú na migráciu: '+', '.join(doc['missing_related']))
    result['deferred']=sorted(set(deferred))
    result['identity_verified']=identity
    if identity:
        result['navigation']={code:_navigation(native,leaf) for code,leaf in
                              (actual or {native['code']:native}).items()}
    result['verified_fields']=not issues
    result['status']='conflict' if not identity else 'partial' if issues else 'verified'
    return result


def observe(documents, fingerprint, expected_ids=None):
    selected=_entries(documents)
    expected_ids=expected_ids or {}
    _check(set(expected_ids)<=set(selected),'new_expected_id_invalid')
    with _connection(fingerprint) as client:
        remote=_read(client,selected,expected_ids)
    return {code:_result(doc,remote.get(code),expected_ids.get(code)) for code,doc in selected.items()}


def complete_related(documents,fingerprint,expected_ids):
    """Only add verified existing xTrek parent codes; the caller fences and journals this PUT separately."""
    selected=_entries(documents)
    _check(set(expected_ids)==set(selected) and all(_positive(ids.get('product_id')) for ids in expected_ids.values()),
           'new_expected_id_invalid')
    result={}
    payload=[]
    with _connection(fingerprint) as client:
        remote=_read(client,selected,expected_ids)
        targets={c for doc in selected.values() for c in doc['payload'].get('related',[])}
        present={p['code'] for p in _sparse(client,targets,'products/simple',filters={'variants_yn':'false'})}
        _check(present<=targets,'new_native_filter_unconfirmed')
        for code,doc in selected.items():
            if doc.get('new_parent_only'):
                result[code] = dict(status='conflict', issues=['new_source_exception_create_only'])
                continue
            parent=remote.get(code)
            current=_result(doc,parent,expected_ids[code])
            planned = set(doc['payload'].get('related', []))
            preserved = set((doc.get('extension_plan') or {}).get('before', {}).get('related', []))
            if not current['identity_verified'] or not set(parent['related']) <= (planned | preserved):
                result[code]=dict(status='conflict',issues=current['issues'] or ['zmenené súvisiace produkty'])
                continue
            wanted=planned & present
            observed=set(parent['related'])
            added=wanted-observed
            if not added:
                result[code]=dict(status='unchanged',issues=[])
                continue
            payload.append(dict(code=code,related=sorted(observed|added)))
            result[code]=dict(status='sent',issues=[])
        if payload:
            transport._request(client,'put','products',payload={'products':payload})
    return result


def _extension_members(root,native,ids,additions):
    """Existing leaves and protected extra generations may coexist; additions must be genuine new variants."""
    desired={r['code']:r for r in root.get('variants',[])}
    missing=set(additions)
    _check(native and native['code']==root['code'] and native['product_id']==ids.get('product_id')
           and native.get('variants_exists_yn') is True and not native.get('set_yn') and not native.get('archived_yn'),
           'new_existing_parent_changed')
    _check(brand_key(native.get('manufacturer') or '')==brand_key(root.get('manufacturer') or '')
           and native['active_yn'] is root['active_yn'],'new_existing_parent_changed')
    actual={v['code']:v for v in native['variants']}
    _check(len(actual)==len(native['variants']) and missing and missing <= set(desired)-set(actual),
           'new_existing_leaf_set_changed')
    anchors=ids.get('variant_ids') or {}
    _check(set(anchors)==(set(desired)&set(actual)) and all(actual[s]['variant_id']==n for s,n in anchors.items()),
           'new_existing_leaf_anchor_changed')
    # An existing managed parent can hold no current Atomer SKU (e.g. older catalog generation).
    # In this workflow at least one source sibling must anchor that native parent's identity.
    _check(anchors,'new_existing_leaf_anchor_missing')
    for code in anchors:
        _check((actual[code].get('ean') or '')==(desired[code].get('ean') or ''),'new_existing_leaf_ean_changed')
    axes={}
    for code,leaf in actual.items():
        values=_parameters(leaf.get('parameters_new'))
        _check(values and all(len(v)==1 for v in values.values()),'new_existing_leaf_axes_missing')
        axes[code]=values
        if code in anchors:
            _check(values==_parameters(desired[code].get('parameters',[])),'new_existing_leaf_axes_changed')
    # Upgates variant combinations use case-insensitive dimension names and value labels.
    old_signatures={_signature(values) for values in axes.values()}
    _check(len(old_signatures)==len(axes),'new_existing_axes_ambiguous')
    signatures=set(old_signatures)
    for code in sorted(missing):
        values=_parameters(desired[code].get('parameters'))
        _check(values and all(len(v)==1 for v in values.values()),'new_axis_names_required')
        # xTrek already has real families with different dimensionality per
        # sibling. Identity is the full unique set of (axis, value) pairs.
        sig=_signature(values)
        _check(sig not in signatures,'new_existing_axis_occupied')
        signatures.add(sig)
    return [dict(desired[code],main_yn=False) for code in sorted(missing)]


def _signature(params):
    def fold(value):
        return ' '.join(value.casefold().split())
    return tuple(sorted((fold(k),tuple(fold(x) for x in v)) for k,v in params.items()))


def _preserved(parent,old_codes):
    """Snapshot every observed existing field apart from generated family aggregation and update times."""
    derived={'last_update_time','variants_stock','variants_availability','variants_availability_id',
             'variants_availability_type','variants'}
    fields={k:v for k,v in parent.items() if k not in derived}
    for field in OTHER_RELATIONS:
        fields.setdefault(field, [])
    leaves={r['code']:{k:v for k,v in r.items() if k!='last_update_time'} for r in parent['variants']
            if r['code'] in old_codes}
    return plain({'parent':fields,'variants':leaves})


def prepare_extensions(documents,fingerprint,checked_at,expected_ids,additions):
    selected=_entries(documents)
    _check(set(expected_ids)==set(additions)==set(selected),'new_expected_id_invalid')
    _check(0<sum(len(chunk) for chunk in additions.values())<=MAX_BATCH,'new_batch_limit')
    with _connection(fingerprint) as client:
        observed=_read(client,selected,expected_ids)
    plans={}
    leaves={}
    for code,doc in selected.items():
        before=observed.get(code)
        try:
            _check(not doc.get('new_parent_only'), 'new_source_exception_create_only')
            new=_extension_members(doc['payload'],before,expected_ids[code],additions[code])
            leaves[code]=new
            plans[code]=dict(status='ready',before=plain(before),payload=dict(code=code,variants=new),
                             product_id=before['product_id'],variant_ids={v['code']:v['variant_id'] for v in before['variants']},
                             issues=[])
        except ReviewError as error:
            plans[code]=dict(status='conflict',issues=[error.code])
    if leaves:
        collisions=_check_absent(leaves,fingerprint,checked_at)
        for code,issues in collisions.items():
            if issues:
                plans[code]=dict(status='conflict',issues=issues)
    return plans


def _extension_anchor(plans):
    _check(isinstance(plans,dict) and 0<len(plans)<=MAX_BATCH,'new_batch_limit')
    _check(all(p.get('status')=='ready' and p['payload']['code']==code and p['payload'].get('variants')
               for code,p in plans.items()),'new_extension_plan_invalid')
    return {code:dict(product_id=p['product_id'],variant_ids=p['variant_ids']) for code,p in plans.items()}


def send_extensions(plans,fingerprint):
    """Call once per journaled intent. Mixed identities and protected field changes stop their own family."""
    from inventory_hub.services.atomer_generations import send as send_updates
    from inventory_hub.services.catalog_import import _assert_payload
    ids=_extension_anchor(plans)
    _check(sum(len(p['payload']['variants']) for p in plans.values())<=MAX_BATCH,'new_batch_limit')
    with _before_write(fingerprint) as client:
        now=_read(client,plans,ids)
    result,payload={},[]
    for code,plan in plans.items():
        old=plan['before']
        current=now.get(code)
        if not current or _full_snapshot(current) != _full_snapshot(old):
            result[code]=dict(status='conflict',issues=['obsah alebo identita rodiny sa pred zápisom zmenili'])
            continue
        patch=plan['payload']
        _check(set(patch)=={'code','variants'},'new_extension_plan_invalid')
        _assert_payload(dict(patch,active_yn=False))
        payload.append(patch)
        result[code]=dict(status='sent',product_id=old['product_id'],issues=[])
    if payload:
        message=send_updates(payload,fingerprint)
        if message:
            for entry in result.values():
                if entry['status']=='sent':
                    entry['issues'].append(message)
    return result


def _full_snapshot(parent):
    result = plain(parent)
    # Older intents predate the independent accessory/gift/set comparison.
    # Newly observed nonempty links still fence the family for a fresh review.
    for field in OTHER_RELATIONS:
        result.setdefault(field, [])
    return result


def _extension_result(doc,live,plan,*,allow_related=False):
    old=plan['before']
    desired_root=doc['payload']
    full_source={v['code'] for v in desired_root['variants']}
    before={v['code'] for v in old['variants']}
    expected=before|{v['code'] for v in plan['payload']['variants']}
    desired=full_source & expected
    if not live:
        return _family_result(doc,None,{'product_id':old['product_id']})
    actual={v['code'] for v in live['variants']}
    # Extra legacy siblings are preserved independently of the Atomer comparison.
    scoped={**live,'variants':[v for v in live['variants'] if v['code'] in desired]}
    previous = set(old['related'])
    wanted_related = set(desired_root.get('related', []))
    # Legacy relations recorded before any write are protected lower down.
    # The full-content comparison covers all new source relations independently.
    scoped['related'] = [code for code in live['related'] if code not in (previous - wanted_related)]
    anchor=dict(product_id=old['product_id'],variant_ids={c:v for c,v in plan['variant_ids'].items() if c in desired})
    variant_plans={v['code']:v for v in plan['payload']['variants']}
    original_defaults = {v['code']: bool(v.get('main_yn')) for v in old['variants']}
    desired_leaves = [
        variant_plans.get(v['code'], {**v, 'main_yn': original_defaults.get(v['code'], False)})
        for v in desired_root['variants'] if v['code'] in desired
    ]
    record=_family_result({**doc,'payload':{**desired_root,'variants':desired_leaves}},scoped,anchor)
    old_protected,new_protected=_preserved(old,before),_preserved(live,before)
    if allow_related:
        # Subsequent source-linked relation work can only add approved edges.
        current=set(live.get('related',[]))
        if previous<=current and current<=previous|wanted_related:
            new_protected['parent']['related']=old_protected['parent']['related']
    if old_protected!=new_protected or actual!=expected:
        record.update(status='conflict',identity_verified=False,verified_fields=False)
        record['issues'].append('zmenené pôvodné údaje rodiny alebo neočakávané varianty')
    record['native']=plain(live)
    record['remaining_variants']=sorted(full_source-actual)
    record['complete_family']=not record['remaining_variants'] and record['status']=='verified'
    return record


def _result(doc,native,expected):
    plan=doc.get('extension_plan')
    if plan:
        result=_extension_result(doc,native,plan,allow_related=True)
        if expected and (result['product_id']!=expected.get('product_id') or any(
                result['variants'].get(k)!=v for k,v in (expected.get('variant_ids') or {}).items())):
            result.update(status='conflict',identity_verified=False,verified_fields=False)
            result['issues'].append('predtým potvrdená väzba variantu sa zmenila')
        return result
    return _family_result(doc,native,expected)


def observe_extensions(documents,fingerprint,plans):
    """Observe uncertain or acknowledged writes; never manufacture an additional variant on recovery."""
    selected=_entries(documents)
    ids=_extension_anchor(plans)
    _check(selected.keys()==plans.keys(),'new_expected_id_invalid')
    with _connection(fingerprint) as client:
        observed=_read(client,selected,ids)
    return {code:_extension_result(doc,observed.get(code),plans[code]) for code,doc in selected.items()}
