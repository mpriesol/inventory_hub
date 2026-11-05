"""Read back an existing native-generation intent without dispatching a catalog or stock write."""
import asyncio

from pydantic import Field, HttpUrl
from sqlalchemy import func, or_, select, text

from inventory_hub.db_models import Product, ProductIdentifier, Shop, SupplierProduct
from inventory_hub.db_models_ext import ShopProduct
from inventory_hub.product_editor_models import AtomerVariantRepair, ProductEditorOverride
from inventory_hub.services import atomer_family_batch as batch
from inventory_hub.services import atomer_generation_recovery as recovery
from inventory_hub.services import atomer_generations as gen
from inventory_hub.services import atomer_proposals as proposals
from inventory_hub.services import atomer_recode as recode
from inventory_hub.services import atomer_review as work
from inventory_hub.services import atomer_variant_repair as repair
from inventory_hub.services import stock_publication_source as transport
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK, barcode_keys, verified_barcodes


def field_changes(before, current):
    changes = {}
    for field in sorted(before.keys() | current.keys()):
        if field in before and field in current and before[field] == current[field]:
            continue
        changes[field] = {label: values[field] for label, values in (("before", before), ("current", current))
                          if field in values}
    return changes


def native_stock(leaf):
    """Keep the raw native responses. A missing or null quantity is not a counted zero."""
    raw = {key: leaf[key] for key in ("stock", "stocks", "stock_position") if key in leaf}
    try:
        quantity = transport._quantity(leaf.get("stock"))
    except transport.SourceError:
        quantity = None
    return {"raw": raw, "quantity": quantity}


def observe_family(doc, observation):
    protected, axes = repair.projection(observation)
    before = doc["before_protected"]
    changed = {}
    for vid, originals in before["leaves"].items():
        current = protected["leaves"].get(vid, [])
        gen.require(len(originals) == 2 and len(current) == 2, "generation_original_leaf_changed")
        diff = {source: result for source, old, new in zip(("product", "variant"), originals, current)
                if (result := field_changes(old, new))}
        if doc["expected_axes"].get(originals[0]["code"]) != axes.get(vid):
            diff["axes"] = {"expected": doc["expected_axes"].get(originals[0]["code"]), "current": axes.get(vid)}
        if diff:
            changed[vid] = diff

    by_code = {leaf["code"]: leaf for leaf in observation["variants"]}
    nested = {leaf["variant_id"]: leaf for leaf in observation["parent"]["variants"]}
    fields = ("ean", "active_yn", "archived_yn", "availability", "availability_id", "weight", "prices", "images",
              "action_currently_yn", "labels")
    additions = {}
    for code, expected in doc["additions"].items():
        leaf = by_code.get(code)
        known_id = doc.get("observed_ids", {}).get(code)
        if leaf is None or leaf["variant_id"] != known_id:
            additions[code] = {"observed_id": known_id, "identity_matches": False}
            continue
        embedded = nested[known_id]
        additions[code] = {
            "observed_id": known_id, "identity_matches": True, "expected": expected,
            "axes": axes[str(known_id)], "product_stock": native_stock(embedded), "variant_stock": native_stock(leaf),
            "product": {key: embedded[key] for key in fields if key in embedded},
            "variant": {key: leaf[key] for key in fields if key in leaf},
        }
    expected_codes = set(doc["expected_axes"])
    return {
        "parent_code": doc["identity"]["parent_code"], "product_id": doc["identity"]["product_id"],
        "parent_changes": field_changes(before["parent"], protected["parent"]), "original_leaf_changes": changed,
        "unexpected_codes": sorted(set(by_code) - expected_codes), "missing_codes": sorted(expected_codes - set(by_code)),
        "additions": additions,
    }


async def observe_generations(db, selection):
    gen.require(selection.phase == "extend" and len({f.product_id for f in selection.families}) == len(selection.families),
                "generation_selection_invalid")
    await work._lock_current_run(db, selection.run_id)
    fingerprint = repair.target_fingerprint("xtrek")
    jobs = []
    for family in selection.families:
        job = await db.get(AtomerVariantRepair, gen.doc_id(selection, family))
        gen.require(job is not None and job.state in ("uncertain", "sending", "verified")
                    and job.document["fingerprint"] == fingerprint, "generation_unverified")
        await recovery.owned(db, job.document, verified=job.state == "verified")
        jobs.append(job)
    observed = await asyncio.to_thread(batch.read_batch, [job.document["identity"] for job in jobs], fingerprint)
    return {"status": "observed", "families": [observe_family(job.document, observed[job.document["identity"]["product_id"]])
                                             for job in jobs]}


class UnreportedStock(gen.Strict):
    selection: gen.Selection
    stock_id: int = Field(gt=0)
    observed_ids: dict[repair.Token, int] = Field(min_length=1, max_length=100)


async def accept_unreported_stock(db, request, mode, actor):
    """Confirm exact native IDs reporting NULL on both read paths. Makes no catalog or stock write."""
    selection = request.selection
    codes = [r.code for f in selection.families for r in f.rows]
    gen.require(selection.phase == 'extend' and len(codes) == len(set(codes)) and set(codes) == set(request.observed_ids)
                and all(i > 0 for i in request.observed_ids.values()) and len(set(request.observed_ids.values())) == len(codes),
                'generation_selection_invalid')
    await work._lock_current_run(db, selection.run_id)
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    fingerprint = repair.target_fingerprint('xtrek')
    jobs = []
    for family in selection.families:
        job = await db.get(AtomerVariantRepair, gen.doc_id(selection, family), with_for_update=True)
        ids = {item.code: request.observed_ids[item.code] for item in family.rows}
        gen.require(job and job.state in ('uncertain', 'sending', 'verified') and job.document['fingerprint'] == fingerprint
                    and job.document.get('observed_ids') == ids and not job.document.get('correction_sent'), 'generation_unverified')
        receipt = {'stock_id': request.stock_id, 'observed_ids': ids}
        if job.state == 'verified':
            gen.require(job.document.get('unreported_stock') == receipt, 'generation_stock_observation_changed')
        else:
            await recovery.owned(db, job.document)
        jobs.append((job, receipt))
    observations = await asyncio.to_thread(batch.read_batch, [job.document['identity'] for job, _ in jobs], fingerprint)
    for job, receipt in jobs:
        observation = observations[job.document['identity']['product_id']]
        doc = {**job.document, 'unreported_stock': receipt}
        ids, issues = gen.observe(doc, observation)
        gen.require(not issues and ids == receipt['observed_ids'], 'generation_other_fields_unverified')
        if mode == 'apply' and job.state != 'verified':
            nested = {v['variant_id']: v for v in observation['parent']['variants']}
            by_code = {v['code']: v for v in observation['variants']}
            raw = {c: {'product': native_stock(nested[i]), 'variant': native_stock(by_code[c])} for c, i in ids.items()}
            job.document = {**doc, 'stock_observation_actor': actor, 'stock_observation': raw}
            result = await gen.finish(db, job, observation, actor)
            gen.require(result['status'] == 'verified', 'generation_other_fields_unverified')
    if mode == 'apply':
        await db.commit()
    return {'status': 'ready' if mode == 'preview' else 'verified', 'count': len(codes),
            'reason': ', '.join(f'{code} / {request.observed_ids[code]}' for code in codes) + f' · xTrek sklad {request.stock_id}: počet neuvedený'}


def reopen_selection(pack):
    first = pack.families[0]
    rows = [r for family in pack.families for r in family.rows]
    gen.require(all(isinstance(f, recode.BarcodeFamily) and (f.run_id, f.source_sha) == (first.run_id, first.source_sha)
                    for f in pack.families), "recode_evidence_changed")
    gen.require(len(rows) <= 100 and len({r.code for r in rows}) == len(rows)
                and all(r.decision == "same_item_supplier_recode" and r.revision > 0 and r.product_id and r.variant_id
                        and not r.manufacturer_or_supplier.username and not r.manufacturer_or_supplier.password for r in rows),
                "recode_evidence_changed")
    return first.run_id, first.source_sha, rows


def may_reopen(record, family, incoming, previous_command, receipt):
    gen.require(all(not r["instruction"] and not r["note"] and r["scope"] != "biketrek" and r["decision"] == "undecided"
                    for r in (record, family)) and family["status"] == "open" and family["revision"] == 0, "review_changed")
    if (record["revision"], record["status"], previous_command, record["result"]) == (
            incoming.revision, "open", "barcode_proof_reopen", receipt):
        return False
    gen.require(record["revision"] == incoming.revision - 1 and record["status"] == "blocked"
                and previous_command == "review_block", "review_changed")
    return True


async def card_activity(db, card_ids):
    """Return current references only. Entries contain Hub identifiers, quantities and states, never customer fields."""
    result = {card: {"balances": [], "reservations": [], "orders": [], "native_orders": [], "stock_work": []} for card in card_ids}
    queries = {
        "balances": "SELECT product_id,warehouse_id,qty_on_hand,qty_reserved,qty_quarantined FROM stock_balances WHERE product_id=ANY(CAST(:ids AS bigint[])) ORDER BY warehouse_id",
        "reservations": "SELECT product_id,id AS reservation_id,warehouse_id,status,quantity,shortage_qty FROM reservations WHERE product_id=ANY(CAST(:ids AS bigint[])) AND status IN ('reserved','backorder') ORDER BY id",
        "orders": """SELECT i.product_id,o.id AS order_id,o.shop_id,i.id AS line_id,i.quantity,o.stock_state FROM shop_order_items i JOIN shop_orders o ON o.id=i.order_id
                    WHERE i.product_id=ANY(CAST(:ids AS bigint[])) AND i.stock_managed AND o.stock_state IN ('pending','reserved') ORDER BY o.id,i.id""",
        "native_orders": """SELECT l.product_id,o.id AS order_id,o.shop_id,l.quantity,o.stock_state FROM stock_native_lines l JOIN shop_orders o ON o.id=l.order_id
                    WHERE l.product_id=ANY(CAST(:ids AS bigint[])) AND o.stock_state IN ('pending','reserved') ORDER BY o.id""",
        "stock_work": """SELECT p.id AS product_id,i.id,i.shop_id,i.status,'stock_sync' AS kind FROM products p JOIN stock_sync_items i ON i.sku=p.sku
                    WHERE p.id=ANY(CAST(:ids AS bigint[])) AND i.status IN ('sending','uncertain') UNION ALL
                    SELECT p.id AS product_id,i.id,b.shop_id,i.status,'stock_publication' AS kind FROM products p JOIN stock_publication_items i ON i.sku=p.sku
                    JOIN stock_publication_batches b ON b.id=i.batch_id WHERE p.id=ANY(CAST(:ids AS bigint[])) AND i.status IN ('sending','uncertain')""",
    }
    for key, query in queries.items():
        for item in (await db.execute(text(query), {"ids": list(result)})).mappings():
            result[item["product_id"]][key].append({k: v for k, v in item.items() if k != "product_id"})
    return result


class ReviewExposure(gen.Strict):
    run_id: int = Field(gt=0)
    revisions: dict[repair.Token, int] = Field(min_length=1, max_length=100)


async def observe_card_activity(db, request):
    await work._lock_current_run(db, request.run_id)
    claims = {}
    for code, revision in request.revisions.items():
        detail = await db.scalar(text("SELECT detail FROM atomer_progress_items WHERE run_id=:r AND code=:code AND state='review'"),
                                 {'r': request.run_id, 'code': code})
        current = await work._record(db, 'item', code)
        gen.require(detail and current['revision'] == revision, 'review_changed')
        candidates = set(detail.get('candidates') or [])
        if detail.get('hub_id'):
            candidates.add(detail['hub_id'])
        if detail.get('product_id'):
            matches = await db.scalars(select(ShopProduct.product_id).join(Shop).where(Shop.code == 'xtrek',
                ShopProduct.external_product_id == detail['product_id'], ShopProduct.external_variant_id == detail.get('variant_id')))
            candidates.update(matches)
        claims[code] = sorted(candidates)
    ids = {card for cards in claims.values() for card in cards}
    return {'run_id': request.run_id, 'candidates': claims, 'activity': await card_activity(db, ids) if ids else {}}


async def reopen_barcodes(db, pack, mode, actor):
    """Reopen only an automated blockage whose exact supplier recode passes the original non-writing preview."""
    run_id, sha, selected = reopen_selection(pack)
    await work._lock_current_run(db, run_id)
    current = await db.scalar(text('SELECT source_sha256 FROM atomer_progress_runs WHERE id=:r'), {'r': run_id})
    gen.require(current == sha, 'variant_source_changed')
    fingerprint = repair.target_fingerprint('xtrek')
    codes = [r.code for r in selected]
    sources = dict((await db.execute(text("""SELECT s->>'code',s FROM atomer_progress_sources p
        CROSS JOIN LATERAL jsonb_array_elements(p.identities) s WHERE p.sha256=:sha AND s->>'code'=ANY(CAST(:codes AS text[]))"""),
        {"sha": sha, "codes": codes})).all())
    receipt = "Overené podklady k oprave EAN: " + repair.digest(pack.model_dump(mode="json"))
    pending, families = [], []
    for family in pack.families:
        entries = []
        for requested in family.rows:
            row = await db.scalar(text("SELECT detail FROM atomer_progress_items WHERE run_id=:r AND code=:c AND state='review'"),
                                  {"r": run_id, "c": requested.code})
            gen.require(row and (row["product_id"], row["variant_id"]) == (str(requested.product_id), str(requested.variant_id)),
                        "recode_mapping_changed")
            saved = await work._record(db, "item", requested.code)
            parent = await work._record(db, "family", row["family_key"])
            previous = await db.scalar(text("SELECT command FROM atomer_review_events WHERE kind='item' AND key=:key AND revision=:rev"),
                                       {"key": requested.code, "rev": saved["revision"]})
            if may_reopen(saved, parent, requested, previous, receipt):
                pending.append(requested.code)
            pinned = await db.scalar(text("SELECT 1 FROM atomer_review_checks WHERE run_id=:r AND key=:key"),
                                     {"r": run_id, "key": row["product_id"] + "/" + row["variant_id"]})
            gen.require(not pinned, "recode_target_protected")
            entries.append(await recode.entry_for(db, requested, row, sources.get(requested.code)))
        ident = repair.native_identity(entries)
        gen.require(all(e["pid"] == str(ident["product_id"]) and e["parent"] == ident["parent_code"] for e in entries),
                    "variant_family_changed")
        await repair.require_target_available(db, "xtrek", ident["parent_code"])
        families.append((family, entries, ident))
    activity = await card_activity(db, {r.card for r in selected})
    gen.require(all(not any(v[key] for key in ("reservations", "orders", "native_orders", "stock_work")) for v in activity.values()),
                "recode_card_in_use")
    if pending:
        native = await asyncio.to_thread(batch.read_batch, [ident for _, _, ident in families], fingerprint)
        for family, entries, ident in families:
            repair.prepare(family, entries, ident, fingerprint, native[ident['product_id']], actor)
    if mode == 'apply':
        for code in pending:
            await repair.outcome(db, code, actor, 'open', receipt, 'barcode_proof_reopen')
        await db.commit()
    return {"status": "ready" if mode == "preview" else "verified", "count": len(selected), "reason": ", ".join(codes)}


class PairCard(gen.Strict):
    id: int = Field(gt=0)
    sku: repair.Token
    link: int = Field(gt=0)
    product_id: str = Field(pattern=r'^[1-9][0-9]{0,19}$')
    variant_id: str | None = Field(default=None, pattern=r'^[1-9][0-9]{0,19}$')
    ean: repair.Token
    source_before: str = Field(max_length=255)
    source_parent: repair.Token
    supplier_product_id: int | None = Field(default=None, gt=0)
    supplier_sku: repair.Token | None = None


class OwnerPair(gen.Strict):
    subject: repair.Token
    revision: int = Field(ge=1)
    other_revision: int = Field(ge=0)
    before_identifier_id: int = Field(gt=0)
    observed_card: PairCard
    correct_owner: PairCard
    manufacturer_or_supplier: HttpUrl
    evidence: str = Field(min_length=40, max_length=1500)


class OwnerSelection(gen.Strict):
    run_id: int = Field(gt=0)
    source_sha: str = Field(pattern=r'^[0-9a-f]{64}$')
    admin_sha: str = Field(pattern=r'^[0-9a-f]{64}$')
    pairs: list[OwnerPair] = Field(min_length=1, max_length=12)


def validate_owner_pair(pair):
    wrong, owner = pair.observed_card, pair.correct_owner
    gen.require(pair.subject == owner.sku and owner.id != wrong.id and owner.link != wrong.link
                and owner.sku.casefold() != wrong.sku.casefold() and owner.source_before == owner.ean
                and not verified_barcodes([wrong.source_before])
                and all(v.ean in verified_barcodes([v.ean]) for v in (wrong, owner))
                and barcode_keys([wrong.ean]).isdisjoint(barcode_keys([owner.ean]))
                and pair.manufacturer_or_supplier.scheme == 'https' and not pair.manufacturer_or_supplier.username
                and not pair.manufacturer_or_supplier.password and all(bool(v.supplier_product_id) == bool(v.supplier_sku)
                for v in (wrong, owner)), 'barcode_owner_proof_invalid')


def native_pair_links(cards, fingerprint):
    """Read the exact sellables, including single leaves on large point-of-sale parents."""
    source = transport._client('biketrek', fingerprint)
    parent_ids = {card.product_id for card in cards}
    variants = {card.variant_id: card for card in cards if card.variant_id is not None}

    def selected(path, key, filters, error):
        try:
            return batch.collect_pages(source, path, key, filters)
        except transport.SourceError as exc:
            if exc.code == 'stock_publication_invalid_response':
                raise transport.SourceError(error, exc.status) from exc
            raise

    try:
        parents = selected('products', 'products', {'product_ids': ';'.join(sorted(parent_ids)), 'variants_yn': 'false'},
                           'barcode_owner_parent_response_invalid')
        leaves = selected('products/variants', 'variants', {'variant_codes': ';'.join(sorted(c.sku for c in variants.values()))},
                          'barcode_owner_variant_response_invalid') if variants else []
    finally:
        source.session.close()
    by_id = {str(p.get('product_id')): p for p in parents}
    by_variant = {str(v.get('variant_id')): v for v in leaves}
    gen.require(len(by_id) == len(parents) and set(by_id) == parent_ids and len(by_variant) == len(leaves)
                and set(by_variant) == set(variants), 'barcode_owner_native_changed')
    result = {}
    for card in cards:
        parent = by_id[card.product_id]
        gen.require(isinstance(parent.get('code'), str) and bool(parent['code'])
                    and parent.get('variants_exists_yn') is (card.variant_id is not None)
                    and transport._messages_ok(parent.get('messages')), 'barcode_owner_native_changed')
        if card.variant_id is None:
            leaf = parent
        else:
            leaf = by_variant[card.variant_id]
            gen.require(str(leaf.get('product_id')) == card.product_id and leaf.get('product_code') == parent['code'],
                        'barcode_owner_native_changed')
        gen.require(leaf.get('code') == card.sku and transport._messages_ok(leaf.get('messages')),
                    'barcode_owner_native_changed')
        result[card.id] = {'product_id': card.product_id, 'variant_id': card.variant_id, 'code': leaf['code'],
                           'ean': leaf.get('ean'), 'parent_code': parent['code'] if card.variant_id else None,
                           'stock': native_stock(leaf)}
    return result


def untouched_other_work(record, group, detail, revision, command):
    """The other owner keeps its untouched proposal. Explicit decisions never authorize this operation."""
    if record['revision'] != revision:
        return False
    if revision == 0:
        return record['status'] == 'open' and not record['instruction'] and not command
    if revision != 1 or record['status'] != 'queued' or command != 'approve_suggestions':
        return False
    scope = record['scope'] if record['scope'] != 'default' else group['scope']
    return record['instruction'] == '\n'.join(proposals.INSTRUCTIONS[k] for k in proposals.suggestions(detail, scope))


async def exact_pair_cards(db, pair, head, native, completed):
    wrong, owner = pair.observed_card, pair.correct_owner
    for expected in (wrong, owner):
        card = await db.get(Product, expected.id, with_for_update=True)
        gen.require(card and card.sku == expected.sku and card.is_active and card.item_type == 'stock', 'barcode_owner_card_changed')
        links = (await db.execute(select(ShopProduct, Shop.code).join(Shop).where(ShopProduct.product_id == card.id)
                                  .with_for_update(of=ShopProduct))).all()
        gen.require(len(links) == 1, 'barcode_owner_shop_changed')
        link, shop = links[0]
        gen.require(shop == 'biketrek' and not link.push_pending and link.id == expected.link
                    and (link.external_code, link.external_product_id, link.external_variant_id, link.parent_code) == (
                        expected.sku, expected.product_id, expected.variant_id, native[card.id]['parent_code'])
                    and bool(link.is_variant) == bool(expected.variant_id), 'barcode_owner_shop_changed')
        await repair.require_target_available(db, 'biketrek', link.parent_code or link.external_code)
        await repair.require_target_available(db, 'xtrek', expected.source_parent)
        override = await db.get(ProductEditorOverride, card.id)
        gen.require(not override or 'eans' not in ((override.data or {}).get('variant') or {}), 'recode_barcode_override')
        if expected.supplier_product_id:
            supplied = await db.get(SupplierProduct, expected.supplier_product_id)
            gen.require(supplied and supplied.supplier_sku == expected.supplier_sku and supplied.ean == expected.ean,
                        'barcode_owner_source_changed')
        detail = await db.scalar(text('SELECT detail FROM atomer_progress_items WHERE run_id=:r AND code=:s'),
                                 {'r': head, 's': expected.sku})
        gen.require(detail and (detail.get('parent') or detail['code']) == expected.source_parent
                    and (detail.get('ean') or '') == expected.source_before, 'barcode_owner_source_changed')
        group = await work._record(db, 'family', detail['family_key'])
        saved = await work._record(db, 'item', expected.sku)
        gen.require(group['status'] == 'open' and group['revision'] == 0 and not group['instruction'] and all(
            not rec['note'] and rec['scope'] != 'biketrek' and rec['decision'] == 'undecided'
            for rec in (group, saved)), 'review_changed')
        command = await db.scalar(text("SELECT command FROM atomer_review_events WHERE kind='item' AND key=:s AND revision=:rev"),
                                  {'s': expected.sku, 'rev': saved['revision']})
        if expected.id == owner.id:
            gen.require(not saved['instruction'] and detail['state'] == 'review' and set(detail.get('candidates') or []) == {owner.id, wrong.id}
                        and saved['status'] == ('open' if completed else 'blocked')
                        and saved['revision'] == pair.revision + int(completed), 'review_changed')
            gen.require(command == ('barcode_owner_verified' if completed else 'review_block'), 'review_changed')
        else:
            gen.require(untouched_other_work(saved, group, detail, pair.other_revision, command), 'review_changed')
    keys = {c.ean.lstrip('0') for c in (wrong, owner)}
    found = list(await db.scalars(select(ProductIdentifier).where(ProductIdentifier.identifier_type.in_(ProductIdentifierService.BARCODE_TYPES),
        or_(ProductIdentifier.product_id.in_((wrong.id, owner.id)), func.ltrim(ProductIdentifier.value, '0').in_(keys))).with_for_update()))
    held = next((v for v in found if v.id == pair.before_identifier_id), None)
    gen.require(held and held.is_primary and held.supplier_id is None and held.value == owner.ean,
                'barcode_owner_identifiers_changed')
    expected = {(wrong.id, wrong.ean), (owner.id, owner.ean)} if completed else {(wrong.id, owner.ean)}
    gen.require(len(found) == len(expected) and {(v.product_id, v.value) for v in found} == expected
                and all(v.is_primary for v in found), 'barcode_owner_identifiers_changed')
    native_codes = {wrong.ean, owner.ean, None, ''}
    gen.require(all(native[c.id]['ean'] in native_codes for c in (wrong, owner)), 'barcode_owner_native_changed')
    return held


async def repair_barcode_owners(db, selection, mode, actor):
    for pair in selection.pairs:
        validate_owner_pair(pair)
    cards = [c for p in selection.pairs for c in (p.observed_card, p.correct_owner)]
    gen.require(len({c.id for c in cards}) == len(cards) and len({c.sku for c in cards}) == len(cards)
                and len({c.ean.lstrip('0') for c in cards}) == len(cards), 'barcode_owner_proof_invalid')
    await work._lock_current_run(db, selection.run_id)
    await db.execute(text('SELECT pg_advisory_xact_lock(:k)'), {'k': IDENTITY_WRITE_LOCK})
    archives = (await db.execute(text('SELECT source_sha256,admin_export_sha256 FROM atomer_progress_runs WHERE id=:r'),
                                 {'r': selection.run_id})).one()
    gen.require(tuple(archives) == (selection.source_sha, selection.admin_sha), 'variant_source_changed')
    native = await asyncio.to_thread(native_pair_links, cards, repair.target_fingerprint('biketrek'))
    activity = await card_activity(db, [c.id for c in cards])
    pending = []
    for pair in selection.pairs:
        receipt = {'operation': 'verified_barcode_owners', 'run': selection.run_id, 'source_sha': selection.source_sha,
                   'admin_sha': selection.admin_sha, 'pair': pair.model_dump(mode='json')}
        key = repair.digest(receipt)
        existing = await db.get(AtomerVariantRepair, key, with_for_update=True)
        gen.require(not existing or existing.state == 'verified', 'barcode_owner_identifiers_changed')
        held = await exact_pair_cards(db, pair, selection.run_id, native, existing is not None)
        if existing:
            continue
        for card in (pair.observed_card, pair.correct_owner):
            facts = activity[card.id]
            gen.require(not any(facts[k] for k in ('reservations', 'orders', 'native_orders', 'stock_work'))
                        and all(v['qty_reserved'] == 0 and v['qty_quarantined'] == 0 for v in facts['balances']), 'recode_card_in_use')
        pending.append((key, pair, held, receipt))
    if mode == 'apply':
        for key, pair, held, receipt in pending:
            wrong, owner = pair.observed_card, pair.correct_owner
            before = {"identifier_id": held.id, "card": held.product_id, "ean": held.value, "primary": held.is_primary}
            held.product_id = owner.id
            await db.flush()
            replacement = await ProductIdentifierService(db).add_identifier(wrong.id, wrong.ean, is_primary=True,
                notes='Kód overený u výrobcu/dodávateľa pri migrácii Atomer')
            result = f"EAN {owner.ean} → karta {owner.id} ({owner.sku}), EAN {wrong.ean} → karta {wrong.id} ({wrong.sku})."
            doc = {**receipt, 'actor': actor, 'before': before, 'after': {str(owner.id): held.id, str(wrong.id): replacement.id},
                   'activity': gen.publication.plain({str(c.id): activity[c.id] for c in (wrong, owner)}),
                   'native': gen.publication.plain({str(c.id): native[c.id] for c in (wrong, owner)})}
            db.add(AtomerVariantRepair(id=key, parent_code=owner.sku[:100], state='verified', document=doc))
            await repair.outcome(db, pair.subject, actor, 'open', result, 'barcode_owner_verified')
        await db.commit()
    return {'status': 'ready' if mode == 'preview' else 'verified', 'count': len(cards),
            'reason': ', '.join(f'{c.sku} / {c.id} / {c.ean}' for c in cards), 'activity': activity, 'native': native}
