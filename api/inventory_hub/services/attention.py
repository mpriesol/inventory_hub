"""One durable incident per kind/resource; shared by Python and authenticated HTTP.

Producers register a typed payload and an internal link builder. No raw exception,
free-form message or caller-supplied URL can enter the UI or diagnostic log.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Callable
from urllib.parse import urlencode

from pydantic import BaseModel, ValidationError
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from inventory_hub import operation_log
from inventory_hub.attention_types import AttentionEvent, SupplierDetails


class AttentionError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status


@dataclass(frozen=True)
class Definition:
    source: str
    severity: str
    payload: type[BaseModel]
    link: Callable[[str, dict], str]
    settings_link: Callable[[str], str] | None = None
    diagnostic_link: Callable[[str, dict], str] | None = None


def supplier_link(scope, _details=None):
    return "/settings/availability?" + urlencode({"supplier": scope})


def supplier_log(scope, details):
    return "/settings/logs?" + urlencode({"supplier": scope, "run_id": details["run_id"]})


# Extend this registry to connect a new service. No schema or center UI change.
DEFINITIONS = {
    "supplier_availability.failed": Definition("supplier_availability", "error", SupplierDetails, supplier_link, supplier_link, supplier_log),
    "supplier_availability.partial": Definition("supplier_availability", "warning", SupplierDetails, supplier_link, supplier_link, supplier_log),
}


def definition(kind):
    if kind not in DEFINITIONS:
        raise AttentionError("attention_unknown_kind", 422)
    return DEFINITIONS[kind]


def _queue(db, event_name, **fields):
    session = db.sync_session
    session.info.setdefault("attention_log", []).append((session.get_nested_transaction(), event_name, fields))


@event.listens_for(Session, "after_commit")
def _committed(session):
    if session.in_nested_transaction():
        return  # A released savepoint is not a committed business result.
    for _savepoint, name, fields in session.info.pop("attention_log", []):
        operation_log.emit(name, component="attention", **fields)


@event.listens_for(Session, "after_soft_rollback")
def _rolled_back(session, transaction):
    if transaction.parent is None:
        session.info.pop("attention_log", None)
        return
    def survives(savepoint):
        while savepoint is not None:
            if savepoint is transaction:
                return False
            savepoint = savepoint.parent
        return True
    session.info["attention_log"] = [entry for entry in session.info.get("attention_log", []) if survives(entry[0])]


async def _lock(db, *parts):
    key = int.from_bytes(hashlib.sha256(("attention:" + ":".join(parts)).encode()).digest()[:8], "big", signed=True)
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def _link(value):
    # Registered builders only; a second check prevents unsafe future extensions.
    if not isinstance(value, str) or not re.fullmatch(r"/(?!/)[a-zA-Z0-9_/?=&.:%-]{0,400}", value) or value.startswith("/api/"):
        raise AttentionError("attention_invalid_link", 422)
    return value


async def report(db, payload: AttentionEvent):
    spec = definition(payload.kind)
    try:
        details = spec.payload.model_validate(payload.details).model_dump(mode="json")
    except ValidationError:
        raise AttentionError("attention_invalid_details", 422) from None
    _link(spec.link(payload.scope, details))
    await _lock(db, payload.kind, payload.scope)
    args = {"kind": payload.kind, "scope": payload.scope}
    old = (await db.execute(text("SELECT * FROM hub_attention_items WHERE kind=:kind AND scope=:scope FOR UPDATE"), args)).mappings().first()
    if old and payload.sequence == old["last_sequence"] and (payload.active != old["active"] or details != old["details"]):
        raise AttentionError("attention_sequence_conflict")
    if old and payload.sequence <= old["last_sequence"]:
        return {"id": old["id"], "revision": old["revision"], "replayed": True}
    stamp = datetime.now(timezone.utc)
    started = payload.active and (not old or not old["active"])
    values = {**args, "sequence": payload.sequence, "active": payload.active, "details": json.dumps(details), "stamp": stamp,
        "occurrences": (old["occurrences"] + 1 if old and old["active"] else 1) if payload.active else 0,
        "episode": (old["episode"] if old else 0) + int(started),
        "first_seen": stamp if started or not old else old["first_seen"],
        "resolved_at": None if payload.active else stamp if old and old["active"] else old["resolved_at"] if old else stamp,
        "ignored_at": old["ignored_at"] if old and old["active"] and payload.active else None}
    result = (await db.execute(text("""INSERT INTO hub_attention_items
        (kind,scope,last_sequence,active,details,first_seen,last_seen,resolved_at,ignored_at,occurrences,episode)
        VALUES (:kind,:scope,:sequence,:active,CAST(:details AS jsonb),:first_seen,:stamp,:resolved_at,:ignored_at,:occurrences,:episode)
        ON CONFLICT (kind,scope) DO UPDATE SET last_sequence=EXCLUDED.last_sequence, active=EXCLUDED.active,
          details=EXCLUDED.details, first_seen=EXCLUDED.first_seen,last_seen=EXCLUDED.last_seen,
          resolved_at=EXCLUDED.resolved_at,ignored_at=EXCLUDED.ignored_at,occurrences=EXCLUDED.occurrences,
          episode=EXCLUDED.episode,revision=hub_attention_items.revision+1
        RETURNING id,revision"""), values)).mappings().one()
    if payload.active or old and old["active"]:
        fields = {"level": spec.severity if payload.active else "info", "attention_id": result["id"],
            "rule_code": payload.kind, "source": spec.source, "scope": payload.scope, "occurrences": values["occurrences"]}
        if spec.source == "supplier_availability":
            fields.update(supplier=payload.scope, run_id=details["run_id"], rows=details["total"],
                          accepted=details["accepted"], rejected=details["rejected"], unknown=details["unknown"], error_code=details["error_code"])
        _queue(db, "attention_raised" if started else "attention_repeated" if payload.active else "attention_resolved", **fields)
    return {**result, "replayed": False}


async def rules(db):
    rows = (await db.execute(text("SELECT * FROM hub_attention_rules ORDER BY kind,scope"))).mappings().all()
    by_key = {(row["kind"], row["scope"]): dict(row) for row in rows if row["kind"] in DEFINITIONS}
    for kind in DEFINITIONS:
        by_key.setdefault((kind, "*"), {"kind": kind, "scope": "*", "revision": 0, "enabled": True, "min_occurrences": 1})
    return {"items": [{**value, "source": definition(value["kind"]).source} for value in by_key.values()]}


async def set_rule(db, kind, scope, payload):
    definition(kind)
    await _lock(db, "rule", kind, scope)
    args = {"kind": kind, "scope": scope}
    old = (await db.execute(text("SELECT revision FROM hub_attention_rules WHERE kind=:kind AND scope=:scope FOR UPDATE"), args)).scalar_one_or_none()
    if (old or 0) != payload.expected_revision:
        raise AttentionError("attention_changed")
    result = (await db.execute(text("""INSERT INTO hub_attention_rules(kind,scope,enabled,min_occurrences)
        VALUES (:kind,:scope,:enabled,:minimum) ON CONFLICT(kind,scope) DO UPDATE SET
        enabled=EXCLUDED.enabled,min_occurrences=EXCLUDED.min_occurrences,
        revision=hub_attention_rules.revision+1,updated_at=now() RETURNING *"""),
        {**args, "enabled": payload.enabled, "minimum": payload.min_occurrences})).mappings().one()
    _queue(db, "attention_rule_updated", rule_code=kind, scope=scope if scope != "*" else "all",
           reason="enabled" if payload.enabled else "disabled", occurrences=payload.min_occurrences)
    return dict(result)


async def action(db, item_id, payload):
    row = (await db.execute(text("SELECT * FROM hub_attention_items WHERE id=:id FOR UPDATE"), {"id": item_id})).mappings().first()
    if not row:
        raise AttentionError("attention_not_found", 404)
    if row["revision"] != payload.expected_revision or not row["active"]:
        raise AttentionError("attention_changed")
    await db.execute(text("UPDATE hub_attention_items SET ignored_at=:stamp,revision=revision+1 WHERE id=:id"),
                     {"id": item_id, "stamp": datetime.now(timezone.utc) if payload.action == "ignore" else None})
    _queue(db, "attention_ignored" if payload.action == "ignore" else "attention_restored", attention_id=item_id,
           rule_code=row["kind"], scope=row["scope"])
    return {"id": item_id, "revision": row["revision"] + 1}


async def overview(db, state="active", source=None, limit=20, offset=0):
    # Evaluate effective rules in SQL so pagination/counts remain exact at any size.
    source_kinds = [kind for kind, spec in DEFINITIONS.items() if not source or spec.source == source]
    condition = {
        "active": "i.active AND i.ignored_at IS NULL AND enabled AND i.occurrences>=minimum",
        "ignored": "i.active AND i.ignored_at IS NOT NULL",
        "muted": "i.active AND (NOT enabled OR i.occurrences<minimum)",
        "resolved": "NOT i.active AND i.episode>0",
    }[state]
    base = """WITH effective AS (SELECT i.*, (COALESCE(g.enabled,true) AND COALESCE(s.enabled,true)) enabled,
        COALESCE(s.min_occurrences,g.min_occurrences,1) minimum FROM hub_attention_items i
        LEFT JOIN hub_attention_rules s ON s.kind=i.kind AND s.scope=i.scope
        LEFT JOIN hub_attention_rules g ON g.kind=i.kind AND g.scope='*') """
    args = {"kinds": source_kinds, "limit": limit, "offset": offset}
    where = " FROM effective i WHERE i.kind=ANY(:kinds) AND " + condition
    total = await db.scalar(text(base + "SELECT count(*)" + where), args)
    rows = (await db.execute(text(base + "SELECT *" + where + " ORDER BY last_seen DESC,id DESC LIMIT :limit OFFSET :offset"), args)).mappings().all()
    items = []
    for row in rows:
        spec = definition(row["kind"])
        items.append({**dict(row), "source": spec.source, "severity": spec.severity,
            "link": _link(spec.link(row["scope"], row["details"])),
            "settings_link": _link(spec.settings_link(row["scope"])) if spec.settings_link else None,
            "log_link": _link(spec.diagnostic_link(row["scope"], row["details"])) if spec.diagnostic_link
                        else "/settings/logs?" + urlencode({"attention_id": row["id"]})})
    return {"items": items, "total": total, "limit": limit, "offset": offset,
            "sources": sorted({spec.source for spec in DEFINITIONS.values()})}
