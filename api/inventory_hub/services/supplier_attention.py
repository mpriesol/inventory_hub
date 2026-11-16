"""Reconcile durable run results into shared attention state, safely after commit.

Replayed every worker cycle, including startup, so a crash between committing a
run and publishing its notification cannot permanently lose the notification.
"""
from sqlalchemy import text
from inventory_hub.attention_types import AttentionEvent
from inventory_hub.services import attention


async def reconcile(db):
    rows = (await db.execute(text("""SELECT s.code, latest.* FROM supplier_availability_settings a
        JOIN suppliers s ON s.id=a.supplier_id JOIN supplier_feeds f ON f.supplier_id=s.id AND f.code=a.feed_key
        JOIN LATERAL (SELECT r.id,r.status,r.error_message,r.error_details,r.items_fetched,r.items_updated,r.items_error FROM supplier_feed_runs r
          WHERE r.feed_id=f.id AND r.error_details->>'workflow'='supplier_availability'
            AND r.status::text IN ('completed','failed')
          ORDER BY r.id DESC LIMIT 1) latest ON true
        LEFT JOIN hub_attention_items failure ON failure.kind='supplier_availability.failed' AND failure.scope=s.code
        LEFT JOIN hub_attention_items partial ON partial.kind='supplier_availability.partial' AND partial.scope=s.code
        WHERE latest.status::text IN ('completed','failed') AND
          (COALESCE(failure.last_sequence,0)<latest.id OR
            (latest.status::text='completed' AND COALESCE(partial.last_sequence,0)<latest.id))"""))).mappings().all()
    for row in rows:
        from inventory_hub.services.supplier_availability import _safe_error
        details = row["error_details"] or {}
        # Manual B2B runs use the same run workflow but predate availability_report.
        result = details.get("availability_report") or {
            "total": row["items_fetched"] or 0, "accepted": row["items_updated"] or 0,
            "rejected": row["items_error"] or 0}
        values = {"run_id": row["id"], "error_code": _safe_error(row["error_message"])}
        for key in ("total", "accepted", "rejected", "unknown"):
            values[key] = result.get(key, 0)
        values["warnings"] = result.get("warnings", [])
        values["coverage"] = result.get("coverage", [])
        failed = str(row["status"]) == "failed"
        await attention.report(db, AttentionEvent(kind="supplier_availability.failed", scope=row["code"],
                               sequence=row["id"], active=failed, details=values))
        # A failed fetch cannot prove an earlier partial-data problem resolved.
        if not failed:
            partial = bool(values["rejected"] or values["unknown"] or values["warnings"] or values["coverage"] or values["error_code"])
            await attention.report(db, AttentionEvent(kind="supplier_availability.partial", scope=row["code"],
                                   sequence=row["id"], active=partial, details=values))
