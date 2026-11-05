"""Read-only, identity-pinned review of xTrek variants outside the Atomer export."""
from collections import Counter, defaultdict
import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import text

from inventory_hub.services.product_identity import barcode_keys


class ReviewError(ValueError):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


class TargetIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    product_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    variant_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    parent: str = Field(min_length=1, max_length=255)
    source_parent: str = Field(min_length=1, max_length=255)
    code: str = Field(min_length=1, max_length=255)
    ean: str = Field(max_length=255)
    axes: dict[str, list[str]]


def parse_targets(fileobj):
    raw = fileobj.read(4_000_001)
    try:
        if len(raw) > 4_000_000:
            raise ValueError()
        pins = TypeAdapter(list[TargetIdentity]).validate_json(raw)
        if not 1 <= len(pins) <= 10_000 or len({p.variant_id for p in pins}) != len(pins):
            raise ValueError()
        result = {}
        for pin in pins:
            if not pin.axes or any(not name or not vals or any(not v for v in vals) for name, vals in pin.axes.items()):
                raise ValueError()
            data = pin.model_dump()
            data["axes"] = normalize_axes(data["axes"])
            result[pin.product_id + "/" + pin.variant_id] = data
        return result
    except (ValidationError, ValueError, TypeError):
        raise ReviewError("invalid_targets", 422) from None


def normalize_axes(axes):
    def norm(x):
        return " ".join(x.split()).casefold()
    return {norm(name):sorted(norm(v) for v in vals) for name,vals in axes.items()}


async def register(db, pins):
    if not pins:
        return
    payload = json.dumps([dict(key=k, identity=v) for k,v in pins.items()], ensure_ascii=False)
    await db.execute(text("""
        INSERT INTO atomer_review_targets(key,identity)
        SELECT d->>'key', d->'identity' FROM jsonb_array_elements(CAST(:pins AS jsonb)) d
        ON CONFLICT(key) DO NOTHING
    """), {"pins":payload})
    mismatch = await db.scalar(text("""
        SELECT EXISTS(SELECT 1 FROM jsonb_array_elements(CAST(:pins AS jsonb)) d
        JOIN atomer_review_targets t ON t.key=d->>'key' WHERE t.identity<>d->'identity')
    """), {"pins":payload})
    if mismatch:
        raise ReviewError("target_pin_changed")


def check_targets(pins, source, targets, rows, links, cards, admin=None, *, admin_complete=False):
    native = {(r["pid"],r["vid"]):r for r in targets if r["vid"]}
    code_counts = Counter(r["code"].casefold() for r in targets)
    product_codes = {r["parent"].casefold() for r in targets if r["parent"]}
    identifiers, gtins = set(), set()
    for item in source:
        for val in (item["code"],item["ean"]):
            if val:
                identifiers.add(val.casefold())
                gtins.update(barcode_keys([val]))
    for code,item in (admin or {}).items():
        for value in (code,item.get("ean")):
            if value:
                identifiers.add(value.casefold())
                gtins.update(barcode_keys([value]))
    families, referenced, ownership = defaultdict(set), defaultdict(list), defaultdict(list)
    titles, parent_states, leaf_states = {}, {}, {}
    for r in rows:
        titles[r["family_key"]] = r["family_name"]
        state = r.get("source_state") or {}
        parent_states[r["family_key"]] = dict(product=state.get("product"),product_origin=state.get("product_origin","unknown"),
                                               product_flags=state.get("product_flags") or {})
        leaf_states[r["code"]] = state
        if r["product_id"] and r["variant_id"]:
            referenced[(r["product_id"],r["variant_id"])].append(r)
            if r["match"] and "parent_differs" not in r["reasons"]:
                families[r["family_key"]].add((r["product_id"],r["target_parent"]))
    for link in links:
        ownership[(link.external_product_id,link.external_variant_id)].append(link.product_id)
    checks = []
    for key,p in pins.items():
        ids = (p["product_id"],p["variant_id"])
        target = native.get(ids)
        family = "v:" + p["source_parent"]
        owners = sorted(set(ownership[ids]))
        warnings, alternatives = [], []
        check = "eligible"
        if not target:
            check = "missing"
        elif (target["parent"],target["code"],target["ean"],normalize_axes(target["axes"])) != (p["parent"],p["code"],p["ean"],p["axes"]):
            check = "changed"
        elif families[family] != {(p["product_id"],p["parent"])}:
            check = "family_changed"
        elif code_counts[p["code"].casefold()] != 1 or p["code"].casefold() in product_codes:
            check = "ambiguous"
        elif any(val and (val.casefold() in identifiers or barcode_keys([val]) & gtins) for val in (p["code"],p["ean"])):
            check = "in_source"
        elif referenced[ids]:
            alternatives = sorted(r["code"] for r in referenced[ids])
            if all("occupied_parameters" in r["reasons"] and "ean_differs" in r["reasons"] for r in referenced[ids]):
                warnings.append("occupied")
            else:
                check = "in_source"
        stock = (target or {}).get("stock")
        if not admin_complete:
            warnings.append("admin_absence_unknown")
        if owners:
            warnings.append("linked")
        if stock is None:
            warnings.append("unknown_stock")
        elif stock != 0:
            warnings.append("stock")
        signature = hashlib.sha256(json.dumps([p,check,owners,stock,alternatives,warnings],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        state = leaf_states.get(p["code"]) or {}
        admin_row = (admin or {}).get(p["code"]) or {}
        origin = state.get("variant_origin","unknown")
        active = state.get("variant")
        if origin=="unknown" and admin_row.get("visibility") is not None:
            active, origin = admin_row["visibility"], "admin_csv"
        source_state = dict(parent_states.get(family,{}),variant=active,variant_origin=origin,
                            variant_flags=state.get("variant_flags") or admin_row.get("flags") or {})
        checks.append({**p,"source_state":source_state,"key":key,"family_key":family,"name":titles.get(family) or p["source_parent"],
                       "check":check,"signature":signature,"warnings":warnings,"alternatives":alternatives,
                       "stock":stock,"card_ids":owners,"hub_sku":cards[owners[0]].sku if len(owners)==1 and owners[0] in cards else None,
                       "shop_url":(target or {}).get("shop_url"),"shop_admin_url":(target or {}).get("shop_admin_url")})
    return checks
