"""Order-only old-code evidence; ordinary product matching stays strict.

An internal_sku string alone is not permission to cross a shop mapping. The
existing editor audit must prove every rename, on this connection and native
leaf, up to the current canonical code. option_set_id is never variant_id.
This reader changes neither identity, historical observations nor stock.
"""
from collections import defaultdict
from dataclasses import dataclass, replace

from sqlalchemy import select

from inventory_hub.product_editor_models import ProductEditorAudit
from inventory_hub.services.product_editor import EditorError
from inventory_hub.services.product_identity import IdentityResolution, RemoteIdentity, mapping_code
from inventory_hub.services.product_publication import fingerprint


def _native(value):
    if isinstance(value, bool):
        return None
    text = str(value or "")
    return text if text.isascii() and text.isdigit() and text[0:1] != "0" and len(text) <= 18 else None


def identity(shop_id, line):
    return RemoteIdentity(shop_id, code=line.get("code", ""),
                          barcodes=(line["ean"],) if line.get("ean") else ())


def candidate(index, line):
    """Only the one blocked exact legacy-alias case may use this exception."""
    incoming = identity(index.shop_id, line)
    resolved = index.resolve(incoming)
    owners = index.exact_internal_sku.get(incoming.code, set())
    if resolved.reasons != ["local_mapping_conflict"] or len(owners) != 1:
        return None
    product_id = next(iter(owners))
    if index.products[product_id].sku == incoming.code:
        return None
    return product_id


@dataclass
class AliasIndex:
    index: object
    shop_code: str
    target_fingerprint: str | None
    audits: dict

    def resolve(self, line):
        index = self.index
        incoming = identity(index.shop_id, line)
        original = index.resolve(incoming)
        product_id = candidate(index, line)
        if product_id is None:
            return original, None

        def conflict(reason):
            return IdentityResolution("conflict", candidate_product_ids=original.candidate_product_ids,
                                      reasons=[reason]), None

        mappings = index.by_product.get(product_id, [])
        if len(mappings) != 1 or not self.target_fingerprint:
            return conflict("order_sku_alias_unverified")
        mapping = mappings[0]
        current = mapping_code(mapping)
        native_id = _native(mapping.external_id)
        if (not native_id or current != index.products[product_id].sku or mapping.is_listed is False
                or (mapping.parent_code or "").casefold() == "xtrek"):
            return conflict("order_sku_alias_unverified")
        # Reuse all ordinary collision, activity and barcode checks against
        # the current mapping. Only the old text code is substituted here.
        resolved = index.resolve(replace(incoming, code=current, is_variant=mapping.is_variant,
                                         external_id=str(mapping.external_id)))
        if resolved.status != "mapped" or resolved.product_id != product_id:
            return resolved, None

        edges = defaultdict(list)
        for audit in self.audits.get(product_id, []):
            before, after = audit.before_data, audit.after_data
            if not isinstance(before, dict) or not isinstance(after, dict):
                continue
            old, new = before.get("sku"), after.get("sku")
            if (after.get("operation") != "verified_code_reconciliation" or not isinstance(old, str)
                    or not isinstance(new, str) or not old or not new or old == new):
                continue
            entries = after.get("native_identities")
            if not isinstance(entries, list):
                continue
            entries = [entry for entry in entries if isinstance(entry, dict)
                       and entry.get("shop_id") == index.shop_id and entry.get("shop") == self.shop_code
                       and entry.get("mapping_id") == mapping.id]
            if len(entries) != 1:
                continue
            entry = entries[0]
            remote, target = entry.get("verified_identity"), entry.get("target")
            if not isinstance(remote, dict) or not isinstance(target, dict):
                continue
            parent_id = _native(remote.get("product_id"))
            leaf_id = _native(remote.get("variant_id")) if mapping.is_variant else parent_id
            parent = mapping.parent_code if mapping.is_variant else new
            if (entry.get("fingerprint") != self.target_fingerprint or _native(entry.get("native_id")) != native_id
                    or leaf_id != native_id or parent_id is None
                    or remote.get("code") != new or target.get("code") != new
                    or remote.get("variant_code") != (new if mapping.is_variant else None)
                    or target.get("variant_code") != (new if mapping.is_variant else None)
                    or remote.get("parent_code") != parent or target.get("parent_code") != parent
                    or (not mapping.is_variant and remote.get("variant_id") is not None)):
                continue
            edges[old].append((new, audit.id, audit.revision, parent_id))

        code, audit_ids, seen, revision, parent_id = incoming.code, [], set(), -1, None
        while code != current:
            if code in seen or len(audit_ids) >= 20 or len(edges[code]) != 1:
                return conflict("order_sku_alias_unverified")
            seen.add(code)
            code, audit_id, next_revision, next_parent = edges[code][0]
            if next_revision <= revision or (parent_id is not None and parent_id != next_parent):
                return conflict("order_sku_alias_unverified")
            audit_ids.append(audit_id)
            revision, parent_id = next_revision, next_parent
        # The historical code plus audited native leaf identifies the variant;
        # the order's option_set_id is a separate, preserved observation.
        option_id = line.get("native_option_set_id")
        if (line.get("has_native_identity") is not True
                or _native(line.get("native_product_id")) != parent_id
                or (mapping.is_variant and _native(option_id) is None)
                or (not mapping.is_variant and option_id is not None)):
            return conflict("order_sku_alias_native_conflict")
        proof = {"old_code": incoming.code, "current_code": current, "shop_id": index.shop_id,
                 "mapping_id": mapping.id, "is_variant": mapping.is_variant, "native_id": native_id,
                 "parent_native_id": parent_id, "audit_ids": audit_ids,
                 "target_fingerprint": self.target_fingerprint}
        return IdentityResolution("mapped", product_id, "verified_sku_alias", [product_id]), proof


async def load(db, shop, index, lines):
    """Batch audit reads only for relevant aliases, without network/cache writes."""
    ids = {product_id for line in lines if (product_id := candidate(index, line)) is not None}
    audits = defaultdict(list)
    target = None
    if ids:
        try:
            target = fingerprint(shop.code)
        except (EditorError, ValueError, OSError):
            pass
        # Read every relevant rename revision to verify multi-hop old aliases.
        rows = await db.scalars(select(ProductEditorAudit).where(ProductEditorAudit.product_id.in_(ids),
            ProductEditorAudit.after_data["operation"].astext == "verified_code_reconciliation")
            .order_by(ProductEditorAudit.revision, ProductEditorAudit.id))
        for row in rows:
            audits[row.product_id].append(row)
    return AliasIndex(index, shop.code, target, audits)
