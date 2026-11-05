"""Resolve Atomer option names from SKU-aligned evidence and the selected shop's native registries."""
from collections import Counter, defaultdict
import re
import unicodedata

from inventory_hub.services import atomer_family_batch as batch
from inventory_hub.services import product_publication_source as publication
from inventory_hub.services import stock_publication_source as transport
from inventory_hub.services.atomer_review_targets import ReviewError


def _fold(value):
    return ' '.join(unicodedata.normalize('NFC', str(value or '')).split()).casefold()


def _parts(path):
    return tuple(_fold(v) for v in str(path).split('/') if v.strip())


def _sk(row):
    values = {d['name'].strip() for d in row.get('descriptions') or []
              if isinstance(d, dict) and (d.get('language') or d.get('language_id')) == 'sk'
              and isinstance(d.get('name'), str) and d['name'].strip()}
    return values.pop() if len(values) == 1 else ''


def _id(value):
    return type(value) is int and value > 0


def category_options(categories, source_paths, aliases=None):
    """Match the whole named path, or a reviewed ancestor substitution followed by an exact path.

    Alias values identify *native paths*, not free-standing IDs. A bare source category can
    otherwise accidentally identify a similarly named node in e.g. a children's department.
    """
    indexed = {}
    for row in categories:
        category_id = row.get('category_id')
        if not _id(category_id) or category_id in indexed:
            raise ReviewError('new_category_registry_invalid')
        indexed[category_id] = row
    paths = {}
    def collect(category_id, trail):
        if category_id in paths:
            return paths[category_id]
        if category_id not in indexed or category_id in trail:
            raise ReviewError('new_category_registry_invalid')
        row = indexed[category_id]
        parent = row.get('parent_id')
        names = collect(parent, trail | {category_id}) if parent is not None else ()
        # Preserve unnamed ancestors as barriers rather than dropping a level.
        name = _fold(_sk(row))
        path = (*names, name)
        paths[category_id] = path
        return path

    suffixes = defaultdict(set)
    for category_id, row in indexed.items():
        path = collect(category_id, set())
        if (not all(path) or row.get('type') not in ('site', 'siteWithProducts')
                or row.get('target_category_id') is not None or row.get('active_yn') is not True):
            continue
        for i in range(len(path)):
            suffixes[path[i:]].add(category_id)
    rules = sorted(((_parts(k), _parts(v)) for k, v in (aliases or {}).items()),
                   key=lambda rule: len(rule[0]), reverse=True)
    resolved, issues = {}, {}
    for original in dict.fromkeys(source_paths):
        path = _parts(original)
        if not path:
            issues[original] = 'missing_source_category'
            continue
        substitution = next(((a,b) for a,b in rules if a and path[:len(a)] == a), None)
        if substitution:
            a, b = substitution
            match = b + path[len(a):]
        else:
            match = path
        ids = suffixes.get(match, set())
        if len(ids) == 1 and (substitution or len(match)>1):
            resolved[original] = next(iter(ids))
        elif ids:
            issues[original] = 'ambiguous_category' if len(ids)>1 else 'category_root_unconfirmed'
        else:
            issues[original] = 'category_unmapped'
    return resolved, issues


def category_contexts(categories, source_paths, resolved, contextual_roots=()):
    """Resolve an explicitly selected broad source ancestor within the primary's known department.

    Use only paths and ancestor IDs that were verified in the category registry. This lets
    an old unisex collection resolve to the existing department-specific collection for
    this item, without putting a women's item into the men's or children's department.
    """
    by_id = {r['category_id']:r for r in categories}
    parents = {i:r.get('parent_id') for i,r in by_id.items()}
    approved = {_parts(k) for k in contextual_roots}
    named = {_parts(k):v for k,v in resolved.items()}
    result = {}
    for primary in dict.fromkeys(source_paths):
        parts = _parts(primary)
        if len(parts)<2 or primary not in resolved or parts[:1] not in approved:
            continue
        seen = set()
        current = resolved[primary]
        while current in parents and current not in seen:
            seen.add(current)
            current = parents[current]
        if parts[:1] in named:
            continue
        # A named collection containing the verified primary is direct evidence for
        # non-gendered source trees. Otherwise require the explicitly gendered branch.
        matching = [i for i in seen if _fold(_sk(by_id[i])) == parts[0]]
        if len(matching)==1:
            department=matching[0]
        elif parts[1] in ('pánske','dámske','men','women'):
            department=named.get(parts[:2])
        else:
            department=None
        if department in seen:
            result[primary] = {primary.split('/')[0].strip():department}
    return result


def _unique_rows(rows):
    index = {}
    for row in rows:
        key = row.get('code')
        if key:
            index[key] = None if key in index else row
    return index


def _same_leaf(source, evidence):
    if source.get('ean') and evidence.get('ean') and str(source['ean']).strip()!=str(evidence['ean']).strip():
        return False
    if source.get('parent') and evidence.get('parent') and source['parent'] != evidence['parent']:
        return False
    same_ean = bool(source.get('ean') and evidence.get('ean'))
    same_title = _fold(source.get('name')) and _fold(source.get('name')) == _fold(evidence.get('name'))
    same_parent = source.get('parent') and source['parent'] == evidence.get('parent')
    return bool(same_ean or same_title or same_parent)


def infer_evidence(source_variants, native_variants=(), named_variants=(), *, field='axes'):
    """Intersect every aligned candidate name, independent of input order and duplicate SKUs.

    Value equality alone cannot prove a name. Require at least three distinct matching
    leaf identities. A contradictory matched option empties the name intersection.
    """
    prefix = 'PARAM_' if field == 'axes' else 'PROPERTY_'
    by_code = _unique_rows(source_variants)
    candidate_names, witnesses, spellings = {}, defaultdict(set), defaultdict(Counter)
    for feed in (native_variants, named_variants):
        for code, named in _unique_rows(feed).items():
            row = by_code.get(code)
            if not row or not named or not _same_leaf(row, named):
                continue
            available = defaultdict(set)
            for name, values in named.get('axes', {}).items():
                if not isinstance(name,str) or not name.strip():
                    continue
                for value in values if isinstance(values,(list,tuple,set)) else [values]:
                    if isinstance(value,str) and _fold(value):
                        available[_fold(value)].add(_fold(name))
                        spellings[_fold(name)][name.strip()] += 1
            for tag, value in row.get(field, {}).items():
                if not re.fullmatch(prefix+r'[0-9]+',tag):
                    continue
                matches = available.get(_fold(value))
                if matches:
                    candidate_names[tag] = candidate_names.get(tag,matches) & matches
                    witnesses[tag].add(code)
    result, ambiguous = {}, {}
    for tag, candidates in candidate_names.items():
        names = [min(spellings[k], key=lambda n:(-spellings[k][n],n)) for k in sorted(candidates)]
        if len(names)==1 and len(witnesses[tag])>=3:
            result[tag] = names[0]
        else:
            ambiguous[tag] = dict(names=names, witnesses=len(witnesses[tag]))
    return result, ambiguous


def infer_axes(source_variants, native_variants=(), named_variants=()):
    return infer_evidence(source_variants, native_variants, named_variants)[0]


def registry_options(availability_rows, label_rows=()):
    """Select a unique exact Slovak availability and the native system label for each mapped flag."""
    availability, labels = defaultdict(set), defaultdict(set)
    for row in availability_rows:
        name, native_id = _fold(_sk(row)), row.get('id')
        if name and _id(native_id):
            availability[name].add(native_id)
    system = {'action':'Akcia','new':'Novinka','sale':'Výpredaj'}
    for row in label_rows:
        native_id = row.get('label_id')
        category = row.get('type')
        if not _id(native_id):
            continue
        if category in system:
            labels[system[category]].add(native_id)
        elif category == 'custom' and _fold(_sk(row)) in {'odporúčaný','odporúčané','odporúčame'}:
            labels['Odporúčaný'].add(native_id)
    return {'availability':{k:next(iter(v)) for k,v in availability.items() if len(v)==1},
            'labels':{k:next(iter(v)) for k,v in labels.items() if len(v)==1}}


AVAILABILITY_TYPES = ('OnRequest','NotAvailable','InStock','Custom')


def _read_option(resource, call, *args, **kwargs):
    try:
        return call(*args,**kwargs)
    except (ReviewError,transport.SourceError) as exc:
        exc.catalog_resource=resource
        raise


def registry_pages(client,resource,*,kind=None):
    if resource not in ('categories','labels','availabilities') or kind is not None and (
            resource!='availabilities' or kind not in AVAILABILITY_TYPES):
        raise ValueError('invalid_registry_resource')
    label=resource if kind is None else 'availabilities_'+kind
    # These registries accept a page selector. current_page_items is native response metadata;
    # only the product catalog endpoints document it as a request parameter.
    return _read_option(label,batch.collect_pages,client,resource,resource,{'type':kind} if kind else {},page_size=None)


def _optional_registry(client,resource,gaps,*,kind=None):
    try:
        return registry_pages(client,resource,kind=kind)
    except transport.SourceError as exc:
        status=getattr(exc,'upstream_status',None)
        if status not in (400,403,404,405,422):
            raise
        # An unsupported collection grants no information. A source flag cannot be published
        # without a verified label ID; known availability IDs are re-read individually below.
        gaps[exc.catalog_resource]={'code':exc.code,'upstream_status':status}
        return []

def read_registry(client, *, availability_ids=()):
    """Fetch non-secret shop options once per catalog run. The caller manages client lifetime and quota."""
    pricing = _read_option('pricing_options',publication._options,client)
    if pricing['prices_with_vat'] is not True:
        raise ReviewError('generation_price_configuration')
    categories=registry_pages(client,'categories')
    gaps={}
    labels=_optional_registry(client,'labels',gaps)
    availability = []
    for kind in AVAILABILITY_TYPES:
        availability.extend(_optional_registry(client,'availabilities',gaps,kind=kind))
    seen = {a.get('id') for a in availability}
    for option in dict.fromkeys(availability_ids):
        if not _id(option):
            raise ReviewError('new_availability_unknown')
        if option not in seen:
            detail = _read_option('availability_by_id',transport._request,client,'get','availabilities/'+str(option))
            rows = detail.get('availabilities') or []
            if (not transport._messages_ok(detail.get('messages')) or len(rows)!=1
                    or rows[0].get('id') != option):
                raise ReviewError('new_availability_unknown')
            availability.extend(rows)
    return {**pricing, **registry_options(availability,labels), 'category_registry':categories,'registry_gaps':gaps}


def fetch_options(client, source_paths, category_aliases=None, availability_ids=(), contextual_roots=()):
    options = read_registry(client,availability_ids=availability_ids)
    source_paths = list(source_paths)
    categories = options.pop('category_registry')
    resolved, issues = category_options(categories,source_paths,category_aliases)
    contexts = category_contexts(categories,source_paths,resolved,contextual_roots)
    return {**options,'categories':resolved,'category_issues':issues,'category_context':contexts}
