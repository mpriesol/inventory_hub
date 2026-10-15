"""Distinguish real supplier choices from fixed attributes on a single SKU."""
from collections import Counter


def normalize_singleton_groups(products, group_sizes=None):
    """Use the complete feed's group sizes, never the user's filtered selection."""
    if group_sizes is None:
        group_sizes = Counter((p.supplier, p.feed_key, p.group_code) for p in products
                              if p.group_code and p.variant_relationship == 'explicit')
    for product in products:
        key = (product.supplier, product.feed_key, product.group_code)
        if product.variant_relationship != 'explicit' or group_sizes.get(key) != 1:
            continue
        known = {(p.name, p.value) for p in product.parameters}
        product.parameters = [*product.parameters, *(a for a in product.variant_attributes
                               if (a.name, a.value) not in known)]
        product.group_code = product.group_name = None
        product.variant_relationship = 'not_provided'
        product.variant_attributes = []
        product.warnings = [w for w in product.warnings if w != 'missing_variant_attributes']
    return products
