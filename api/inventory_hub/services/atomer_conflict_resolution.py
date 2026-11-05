"""Read back an existing native-generation intent without dispatching a catalog or stock write."""
import asyncio

from inventory_hub.product_editor_models import AtomerVariantRepair
from inventory_hub.services import atomer_family_batch as batch
from inventory_hub.services import atomer_generation_recovery as recovery
from inventory_hub.services import atomer_generations as gen
from inventory_hub.services import atomer_review as work
from inventory_hub.services import atomer_variant_repair as repair
from inventory_hub.services import stock_publication_source as transport


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
