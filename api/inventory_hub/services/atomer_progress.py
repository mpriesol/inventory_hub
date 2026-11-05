"""Read-only catalog reconciliation of every sellable row of a buyers export."""
from collections import defaultdict
from os.path import commonprefix
import json
import zipfile

from lxml import etree

from inventory_hub.adapters.atomer_content import AtomerSourceError, MAX_ROWS, _preflight, _text
from inventory_hub.services.product_link_source import _navigation
from inventory_hub.services.atomer_source_state import flag
from inventory_hub.services.product_identity import RemoteIdentity, barcode_keys
from inventory_hub.services.migration_upgates_export import ExportError, build_remote_export


def _identifier(node, field):
    value = _text(node, field).strip()
    if len(value) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise AtomerSourceError("invalid_source_identity")
    return value


def parse_source(fileobj):
    digest, _ = _preflight(fileobj)
    by_code = {}
    total = 0
    try:
        root = None
        for event, node in etree.iterparse(fileobj, events=("start", "end"), resolve_entities=False,
                                          load_dtd=False, no_network=True, huge_tree=False):
            if root is None:
                root = node
                if root.tag != "PRODUCTS":
                    raise AtomerSourceError("invalid_buyers")
            if event != "end" or node is root:
                continue
            if node.getparent() is not root:
                if node.tag == "PRODUCT":
                    raise AtomerSourceError("invalid_buyers")
                continue
            total += 1
            if total > MAX_ROWS or node.tag != "PRODUCT":
                raise AtomerSourceError("invalid_buyers")
            code = _identifier(node, "PRODUCT_ID")
            if not code:
                raise AtomerSourceError("missing_source_code")
            axes = {}
            for param in node.findall("VARIANT_PARAMETERS/*"):
                if param.tag in axes or len(param):
                    raise AtomerSourceError("ambiguous_source_axes")
                axes[param.tag] = (param.text or "").strip()
            data = dict(code=code, parent=_identifier(node, "PARENT_ID"),
                        name=_text(node, "PRODUCT_NAME"), ean=_identifier(node, "EAN"),
                        brand=_text(node, "MANUFACTURER"), axes=axes)
            visibility = flag(_text(node, "VISIBILITY"))
            if code in by_code:
                previous = by_code[code]
                previous["duplicates"] += 1
                if previous["source_visibility"] != visibility:
                    previous["visibility_conflict"] = True
                if any(previous[k] != value for k, value in data.items()):
                    previous["conflict"] = True
            else:
                by_code[code] = dict(data, duplicates=1, conflict=False, source_visibility=visibility, visibility_conflict=False)
            node.clear()
            while node.getprevious() is not None:
                del root[0]
    except etree.XMLSyntaxError:
        raise AtomerSourceError("invalid_buyers") from None
    if not by_code:
        raise AtomerSourceError("empty_buyers")
    return digest, total, list(by_code.values())


def _catalog_rows(dataset, shop="xtrek"):
    with build_remote_export(shop, dataset) as raw, zipfile.ZipFile(raw) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        for page in manifest["pages"]:
            yield from json.loads(archive.read(page["file"]))[dataset]


def remote_catalog():
    from inventory_hub.routers.upgates_sync import _cache_load
    from inventory_hub.services import atomer_catalog_observation
    xtrek = atomer_catalog_observation.catalog(_cache_load("xtrek"))
    return xtrek, bike_catalog(_catalog_rows("products", "biketrek"))


def bike_catalog(products):
    result = {}
    for parent in products:
        pid = str(parent["product_id"])
        for leaf in parent.get("variants") or [parent]:
            vid = str(leaf["variant_id"]) if leaf is not parent else None
            key = (pid, vid)
            if key in result:
                raise ExportError("catalog_changed")
            result[key] = dict(code=leaf.get("code"), parent=parent.get("code") if vid else None,
                               **_navigation(parent, leaf))
    return result


def flatten_catalog(products, variants):
    details = {str(v["variant_id"]): v for v in variants}
    leaves = []
    for p in products:
        for child in p.get("variants") or [p]:
            variant = child is not p
            pid, code = str(p["product_id"]), child["code"]
            vid = str(child["variant_id"]) if variant else None
            params = {}
            if variant:
                v = details.pop(vid, None)
                if (not v or str(v.get("product_id")) != pid or v.get("code") != code
                        or (v.get("ean") or "") != (child.get("ean") or "")):
                    raise ExportError("catalog_changed")
                for par in v.get("parameters") or []:
                    name = par.get("name", {}).get("sk")
                    values = [entry.get("sk", "") for entry in par.get("values") or []]
                    if name and values:
                        params.setdefault(_norm(name), []).extend(_norm(value) for value in values)
            leaves.append(dict(code=code or "", pid=pid, vid=vid, parent=(p.get("code") or "") if variant else "",
                               stock=child.get("stock"), availability=child.get("availability"), ean=child.get("ean") or "", brand=p.get("manufacturer") or "", axes=params,
                               title=next((d.get("title") for d in p.get("descriptions") or []
                                           if d.get("language") == "sk" and d.get("title")), None),
                               **_navigation(p, child)))
    if details:
        raise ExportError("catalog_changed")
    return leaves


def _norm(value):
    return " ".join(value.split()).casefold()


def _brand(value):
    return "".join(c for c in value.casefold() if c.isalnum())


def _variant(row):
    return bool(row["parent"] and row["parent"] != row["code"])


def _conflicts(source, target):
    reasons = set()
    if _variant(source) != (target["vid"] is not None):
        reasons.add("kind_differs")
    if _brand(source["brand"]) and _brand(target["brand"]) and _brand(source["brand"]) != _brand(target["brand"]):
        reasons.add("brand_differs")
    if source["gtins"] and target["gtins"] and source["gtins"].isdisjoint(target["gtins"]):
        reasons.add("ean_differs")
    return reasons


def match_catalog(sources, targets):
    """Unambiguous code/GTIN first, then unique complete axes in an anchored family."""
    code_index, gtin_index, families = defaultdict(set), defaultdict(set), defaultdict(set)
    for i, row in enumerate(targets):
        if row["code"]:
            code_index[row["code"].casefold()].add(i)
        row["gtins"] = barcode_keys([row["ean"]])
        for key in row["gtins"]:
            gtin_index[key].add(i)
        if row["vid"]:
            families[row["parent"]].add(i)
    pairs, evidence, problems = {}, {}, defaultdict(set)
    claims = defaultdict(set)
    for row in sources:
        code = row["code"]
        row["gtins"] = barcode_keys([row["ean"]])
        if row["conflict"]:
            problems[code].add("duplicate_source")
        by_code = code_index.get(code.casefold(), set())
        by_ean = {i for k in row["gtins"] for i in gtin_index[k]}
        found = by_code | by_ean
        if len(found) > 1:
            problems[code].add("multiple_targets")
        elif found:
            i, = found
            evidence[code] = i
            problems[code].update(_conflicts(row, targets[i]))
            claims[i].add(code)
            if not problems[code]:
                pairs[code] = i
    for codes in claims.values():
        if len(codes) > 1:
            for code in codes:
                problems[code].add("target_claimed")
                pairs.pop(code, None)
    witnesses, parent_map = defaultdict(list), defaultdict(set)
    for row in sources:
        if row["code"] not in pairs or not _variant(row):
            continue
        target = targets[pairs[row["code"]]]
        parent_map[row["parent"]].add(target["parent"])
        for k, val in row["axes"].items():
            names = [name for name, vals in target["axes"].items() if vals == [_norm(val)]]
            if len(names) == 1:
                witnesses[k].append(names[0])
    axes = {k: vs[0] for k, vs in witnesses.items() if len(vs) >= 3 and len(set(vs)) == 1}
    axis_claims = defaultdict(list)
    methods = {k: "code_or_ean" for k in pairs}
    for row in sources:
        code, source_parent = row["code"], row["parent"]
        if not _variant(row) or problems[code]:
            continue
        possible = parent_map[source_parent]
        if source_parent in families:
            possible = possible | {source_parent}
        if len(possible) > 1:
            problems[code].add("parent_differs")
            continue
        mapped_axes = {axes[k]: [_norm(v)] for k, v in row["axes"].items() if k in axes}
        if code in pairs:
            target = targets[pairs[code]]
            if any(v != target["axes"].get(k) for k, v in mapped_axes.items()):
                problems[code].add("parameters_differ")
            continue
        if len(possible) != 1 or not mapped_axes or len(mapped_axes) != len(row["axes"]):
            continue
        parent, = possible
        candidates = [i for i in families[parent] if targets[i]["axes"] == mapped_axes]
        if len(candidates) > 1:
            problems[code].add("multiple_targets")
        elif candidates:
            i, = candidates
            evidence[code] = i
            reasons = _conflicts(row, targets[i])
            if reasons:
                problems[code].update(reasons | {"occupied_parameters"})
            elif i in claims:
                problems[code].add("target_claimed")
            else:
                axis_claims[i].append(code)
    for i, codes in axis_claims.items():
        if len(codes) == 1:
            pairs[codes[0]] = i
            methods[codes[0]] = "family_parameters"
        else:
            for code in codes:
                problems[code].add("target_claimed")
    return pairs, evidence, methods, problems


def biketrek_link(card_id, reasons, bike_links, bike_targets):
    rows = bike_links.get(card_id, [])
    link = rows[0] if len(rows) == 1 else None
    evidence = None
    if link:
        evidence = {"code":link.external_code, "parent":link.parent_code,
                    "product_id":link.external_product_id, "variant_id":link.external_variant_id,
                    "is_variant":link.is_variant, "link_id":link.id}
    if reasons & {"card_identity_conflict", "multiple_cards", "card_brand_differs", "card_ean_differs", "duplicate_source"}:
        if evidence:
            evidence["reason"] = "card_conflict"
        return "review", evidence
    if not card_id:
        return "no_card", None
    if not rows:
        return "unlinked", None
    if (not link or not link.external_product_id or not link.external_code
            or bool(link.external_variant_id) != bool(link.is_variant)):
        if evidence:
            evidence["reason"] = "incomplete_link"
        return "review", evidence
    target = bike_targets.get((link.external_product_id, link.external_variant_id))
    if not target:
        evidence["reason"] = "missing_native"
        return "review", evidence
    evidence.update(shop_url=target["shop_url"], shop_admin_url=target["shop_admin_url"])
    if target["code"] != link.external_code or (link.is_variant and target["parent"] != link.parent_code):
        evidence["reason"] = "changed_identity"
        return "review", evidence
    return "linked", evidence


def reconcile(sources, targets, card_index, links, overrides, bike_links, bike_targets):
    pairs, evidence, methods, conflicts = match_catalog(sources, targets)
    by_native, by_card = defaultdict(list), defaultdict(list)
    for link in links:
        by_native[(link.external_product_id, link.external_variant_id)].append(link)
        by_card[link.product_id].append(link)
    rows = []
    for source in sources:
        code = source["code"]
        reasons = set(conflicts[code])
        candidate = targets[evidence[code]] if code in evidence else None
        target = targets[pairs[code]] if code in pairs else None
        lookups = [(code, source["ean"])]
        explicit = []
        if target:
            lookups.append((target["code"], target["ean"]))
            explicit = by_native[(target["pid"], target["vid"])]
        ids = {link.product_id for link in explicit}
        for lookup_code, ean in lookups:
            resolution = card_index.resolve(RemoteIdentity(card_index.shop_id, code=lookup_code, barcodes=(ean,)))
            ids.update(resolution.candidate_product_ids)
            if resolution.status == "conflict":
                reasons.add("card_identity_conflict")
        card_id = next(iter(ids)) if len(ids) == 1 else None
        if len(ids) > 1:
            reasons.add("multiple_cards")
        card = card_index.products.get(card_id)
        if card:
            edit = overrides.get(card_id) or {}
            brand = (edit.get("common") or {}).get("brand") or card.brand or ""
            if _brand(source["brand"]) and _brand(brand) and _brand(source["brand"]) != _brand(brand):
                reasons.add("card_brand_differs")
            eans = (edit.get("variant") or {}).get("eans")
            existing = barcode_keys(eans if isinstance(eans,list) else card_index.product_barcodes.get(card_id, set()))
            if existing and source["gtins"] and existing.isdisjoint(source["gtins"]):
                reasons.add("card_ean_differs")
            if by_card[card_id] and not explicit:
                reasons.add("different_shop_link")
        if explicit and (len(explicit) != 1 or explicit[0].external_code != target["code"]
                or bool(explicit[0].is_variant) != bool(target["vid"])
                or (target["vid"] and explicit[0].parent_code != target["parent"])):
            reasons.add("link_identity_differs")
        if reasons:
            state = "review"
        elif explicit and card:
            state = "linked"
        elif target:
            state = "needs_link" if card else "needs_card"
        else:
            state = "needs_target" if card else "needs_both"
        detail = target or candidate or {}
        bike_state, bike = biketrek_link(card_id, reasons, bike_links, bike_targets)
        ambiguous_card = bool(reasons & {"card_identity_conflict", "multiple_cards", "card_brand_differs", "card_ean_differs", "duplicate_source"})
        card_state = "review" if ambiguous_card else "exists" if card else "missing"
        rows.append({"code":code, "card_state":card_state,
                     "family_key":("v:"+source["parent"]) if _variant(source) else ("s:"+code),
                     "variant_label":" / ".join(source["axes"].values()), "family_title":(target or {}).get("title"),
                     "shop_url":detail.get("shop_url"), "shop_admin_url":detail.get("shop_admin_url"), "kind":"variant" if _variant(source) else "standalone", "state":state,
                     "parent":source["parent"], "name":source["name"], "ean":source["ean"],
                     "hub_id":card_id, "hub_sku":card.sku if card else None,
                     "candidates":sorted(ids) if len(ids)>1 else [],
                     "target_code":detail.get("code"), "target_parent":detail.get("parent"),
                     "target_ean":detail.get("ean"), "product_id":detail.get("pid"), "variant_id":detail.get("vid"),
                     "match":methods.get(code), "reasons":sorted(reasons),
                     "bike_state":bike_state, "bike":bike})
    families = defaultdict(list)
    for row in rows:
        families[row["family_key"]].append(row)
    for members in families.values():
        titles = {row.pop("family_title") for row in members} - {None, ""}
        first = members[0]
        if len(titles) == 1 and first["kind"] == "variant":
            title = titles.pop()
        elif len(members) == 1:
            title = first["name"] or first["code"]
        else:
            prefix = commonprefix([row["name"].split() for row in members])
            title = " ".join(prefix).rstrip(" |/,–-")
            if len(prefix) <= 1:
                title = (title + " " + first["parent"]).strip()
        for row in members:
            row["family_name"] = title or first["parent"] or first["code"]
    return rows
