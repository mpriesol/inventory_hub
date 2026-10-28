"""Order discovery evidence for regular stock publication, without network I/O.

Notifications prove that a known order needs attention. They never advance the
collector's full-scan cursor or certify that no other order changed upstream.
The caller must additionally check local processing and native stock evidence.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from inventory_hub.order_collection_models import OrderCollectionRun, OrderCollectionSettings


REGISTRATION_MAX_AGE_SECONDS = 6 * 3600
SCHEDULER_GRACE_SECONDS = 60


def evaluate_collection(collector, webhook, delta, expected_target, max_order_age_seconds, *, at):
    """Keep polling strict; an explicitly registered webhook uses bounded catchup."""
    result = []
    # A known notification cannot be ignored by subsequently disabling webhooks.
    if webhook and (webhook.get("pending_count", 0) or webhook.get("review_count", 0)):
        result.append("stock_sync_order_notifications_pending")
    if (collector is None or not collector.enabled or not expected_target
            or collector.target_fingerprint != expected_target or collector.last_error
            or collector.last_completed_at is None or collector.cursor_at is None):
        return result + ["stock_sync_orders_stale"]
    if not webhook or not webhook.get("enabled"):
        cutoff = at - timedelta(seconds=max_order_age_seconds)
        if collector.last_completed_at < cutoff or collector.cursor_at < cutoff:
            result.append("stock_sync_orders_stale")
        return result

    registered = webhook.get("registered_at")
    checked = webhook.get("registration_checked_at")
    if (webhook.get("target_fingerprint") != expected_target
            or webhook.get("registration_status") != "registered" or webhook.get("last_error")
            or registered is None or checked is None or registered > checked or checked > at
            or checked < at - timedelta(seconds=REGISTRATION_MAX_AGE_SECONDS)):
        return result + ["stock_sync_order_webhook_not_ready"]

    interval = webhook.get("catchup_interval_seconds")
    timeout = (delta.configuration_snapshot or {}).get("run_timeout_seconds") if delta else None
    if (type(interval) is not int or not 300 <= interval <= 86400
            or type(timeout) is not int or not 30 <= timeout <= 180
            or delta.status != "completed" or delta.mode != "delta"
            or delta.settings_revision != collector.revision
            or delta.completed_at is None or delta.started_at < registered
            or not delta.started_at <= delta.completed_at <= at
            or collector.cursor_at < delta.started_at or collector.cursor_at > at
            or collector.last_completed_at < delta.completed_at):
        return result + ["stock_sync_orders_stale"]
    # A due bounded reconciliation may run immediately before its delta pass.
    # Both passes have a timeout; silence or a fresh unrelated webhook cannot
    # prolong this deadline. Failed scans remain blocked by last_error above.
    deadline = delta.completed_at + timedelta(seconds=interval + 2 * timeout + SCHEDULER_GRACE_SECONDS)
    if at > deadline:
        result.append("stock_sync_orders_stale")
    return result


async def collection_blockers(db, shop_id, expected_target, max_order_age_seconds, *, at=None):
    """Read current durable evidence; never perform an upstream health probe."""
    from inventory_hub.services.order_webhooks import delivery_health

    at = at or datetime.now(timezone.utc)
    collector = await db.scalar(select(OrderCollectionSettings).where(
        OrderCollectionSettings.shop_id == shop_id).execution_options(populate_existing=True))
    webhook = await delivery_health(db, shop_id, at=at)
    delta = None
    if collector is not None and webhook and webhook.get("enabled"):
        delta = await db.scalar(select(OrderCollectionRun).where(
            OrderCollectionRun.shop_id == shop_id,
            OrderCollectionRun.settings_revision == collector.revision,
            OrderCollectionRun.status == "completed", OrderCollectionRun.mode == "delta")
            .order_by(OrderCollectionRun.started_at.desc(), OrderCollectionRun.id.desc())
            .limit(1).execution_options(populate_existing=True))
    return evaluate_collection(collector, webhook, delta, expected_target, max_order_age_seconds, at=at)
