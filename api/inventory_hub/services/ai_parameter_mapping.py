"""Category-scoped parameter names; source facts and variant values stay intact."""
import unicodedata


def name_key(value: str) -> str:
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', value.casefold())
                           if not unicodedata.combining(c)).split())


def matching_definition(name, definitions):
    matches = [p for p in definitions if name_key(p['name']) == name_key(name)]
    return matches[0] if len(matches) == 1 else None


def parameter_mapping(content, context):
    """Return a one-to-one source-axis → registry mapping and blocking diagnostics.

    AI may propose a semantic name mapping with explicit source_name provenance.
    It cannot remap an already recognised axis or merge two distinct source axes.
    Every selected SKU must still supply the exact original variant value.
    """
    definitions = [p for p in (context['resolved'].get('category') or {}).get('parameters', [])
                   if p.get('approved', True)]
    sources = {a['name'] for p in context['facts'] for a in p.get('variant_attributes', [])}
    mapping, errors = {}, []
    for source in sources:
        definition = matching_definition(source, definitions)
        if definition:
            mapping[source] = definition['name']
    for value in content.parameters:
        if value.product_id is None or not value.source_name:
            continue
        source = value.source_name
        definition = matching_definition(value.name, definitions)
        if source not in sources or not definition or (source in mapping and mapping[source] != definition['name']):
            errors.append('ai_parameter_mapping_conflict:' + value.name)
        else:
            mapping[source] = definition['name']
    # Case/diacritic spelling aliases across SKUs are safe, two axes on one SKU
    # are not. Semantic aliases must be consistent across the whole family.
    for product in context['facts']:
        targets = [mapping[a['name']] for a in product.get('variant_attributes', []) if a['name'] in mapping]
        if len(targets) != len(set(targets)):
            errors.append('ai_parameter_mapping_conflict:' + str(product['id']))
    return mapping, errors


def canonical_attributes(attributes, mapping):
    return [{**a, 'name': mapping.get(a['name'], a['name'])} for a in attributes]
