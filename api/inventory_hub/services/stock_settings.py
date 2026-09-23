"""Resolve operational defaults and authorize automatic local stock processing."""
from datetime import datetime, timezone
import hashlib
import json
from pydantic import ValidationError
from sqlalchemy import select
from inventory_hub.db_models import Shop, Warehouse
from inventory_hub.order_stock_models import OrderStockPolicy
from inventory_hub.order_collection_models import OrderCollectionSettings
from inventory_hub.stock_settings_models import StockWarehouseSettings, StockShopSettings
from inventory_hub.stock_settings_types import OperationalValues


class SettingsError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def now():
    return datetime.now(timezone.utc)


async def _one(db, model, condition, lock=None):
    statement = select(model).where(condition).execution_options(populate_existing=True)
    if lock:
        statement = statement.with_for_update(read=lock == "share")
    return await db.scalar(statement)


async def _warehouse(db, code=None, identifier=None, lock=None):
    row = await _one(db, Warehouse, Warehouse.code == code if code is not None else Warehouse.id == identifier, lock)
    if row is None or not row.is_active:
        raise SettingsError("stock_settings_warehouse_not_found", 404)
    return row


async def _shop(db, code, lock=None):
    row = await _one(db, Shop, Shop.code == code, lock)
    if row is None or not row.is_active or row.platform != "upgates":
        raise SettingsError("stock_settings_shop_not_found", 404)
    return row


def _values(warehouse, shop=None):
    try:
        return OperationalValues(**{**OperationalValues().model_dump(), **(warehouse.values if warehouse else {}),
                                    **(shop.overrides if shop else {})}).model_dump()
    except (ValidationError, TypeError):
        raise SettingsError("stock_settings_invalid_values", 422) from None


def _warehouse_public(warehouse, row):
    return {"warehouse_code": warehouse.code, "revision": row.revision if row else 0,
            "values": _values(row), "processing_paused": row.processing_paused if row else False}


def _shop_public(row):
    return {"revision": row.revision if row else 0, "overrides": row.overrides if row else {},
            "mode": row.mode if row else "manual", "automation_starts_at": row.automation_starts_at if row else None,
            "issue_starts_at": row.issue_starts_at if row else None,
            "authorized_policy_revision": row.authorized_policy_revision if row else None,
            "target_fingerprint": row.target_fingerprint if row else None}


async def effective(db, shop_id, lock=False):
    """Caller locks Shop/policy first. Parent warehouse lock also protects absent settings rows."""
    policy = await _one(db, OrderStockPolicy, OrderStockPolicy.shop_id == shop_id)
    if policy is None:
        raise SettingsError("stock_settings_policy_required")
    warehouse = await _warehouse(db, identifier=policy.warehouse_id, lock="share" if lock else None)
    warehouse_row = await _one(db, StockWarehouseSettings, StockWarehouseSettings.warehouse_id == warehouse.id,
                               "share" if lock else None)
    shop_row = await _one(db, StockShopSettings, StockShopSettings.shop_id == shop_id, "share" if lock else None)
    result = {"warehouse_id": warehouse.id, "warehouse_code": warehouse.code,
              "warehouse_revision": warehouse_row.revision if warehouse_row else 0,
              "shop_revision": shop_row.revision if shop_row else 0,
              "values": _values(warehouse_row, shop_row),
              "sources": {key: "shop" if shop_row and key in shop_row.overrides else "warehouse"
                          for key in OperationalValues.model_fields},
              "processing_paused": warehouse_row.processing_paused if warehouse_row else False,
              **{key: value for key, value in _shop_public(shop_row).items() if key not in ("revision", "overrides")}}
    result["configuration_hash"] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":"),
        default=lambda value: value.isoformat()).encode()).hexdigest()
    # Runtime throttling is separate from the operator configuration revision/hash.
    retry_after = shop_row.processing_retry_after_at if shop_row else None
    collector = await _one(db, OrderCollectionSettings, OrderCollectionSettings.shop_id == shop_id)
    from inventory_hub.services.order_collection import target_fingerprint
    shop = await _one(db, Shop, Shop.id == shop_id)
    current_target = target_fingerprint(shop.code) if shop and shop.is_active and shop.platform == "upgates" else None
    if (collector and collector.target_fingerprint == current_target
            and (result["mode"] == "manual" or collector.target_fingerprint == result["target_fingerprint"])
            and collector.last_error == "order_collection_rate_limited" and collector.retry_after_at):
        retry_after = max(retry_after, collector.retry_after_at) if retry_after else collector.retry_after_at
    result["retry_after_at"] = retry_after
    error = None
    if result["mode"] != "manual":
        if result["processing_paused"]:
            error = "order_processing_paused"
        elif result["authorized_policy_revision"] != policy.revision:
            error = "order_processing_policy_changed"
        else:
            if shop is None or not shop.is_active or shop.platform != "upgates":
                error = "order_processing_shop_unavailable"
            elif current_target != result["target_fingerprint"]:
                error = "order_processing_target_changed"
            elif retry_after and retry_after > now():
                error = "order_processing_retry_later"
    result["processing_error"] = error
    result["processing_ready"] = result["mode"] != "manual" and error is None
    return result


async def warehouse_settings(db, warehouse_code):
    warehouse = await _warehouse(db, code=warehouse_code)
    row = await _one(db, StockWarehouseSettings, StockWarehouseSettings.warehouse_id == warehouse.id)
    return _warehouse_public(warehouse, row)


async def options(db, shop_code):
    shop = await _shop(db, shop_code)
    warehouses = (await db.scalars(select(Warehouse).where(Warehouse.is_active.is_(True)).order_by(Warehouse.code))).all()
    policy = await _one(db, OrderStockPolicy, OrderStockPolicy.shop_id == shop.id)
    warehouse = next((item for item in warehouses if policy and item.id == policy.warehouse_id), None)
    row = await _one(db, StockShopSettings, StockShopSettings.shop_id == shop.id)
    return {"shop": {"code": shop.code, "name": shop.name},
            "warehouses": [{"id": item.id, "code": item.code, "name": item.name} for item in warehouses],
            "policy": {"warehouse_id": warehouse.id, "warehouse_code": warehouse.code, "starts_at": policy.starts_at,
                       "revision": policy.revision} if warehouse else None,
            "warehouse": await warehouse_settings(db, warehouse.code) if warehouse else None,
            "shop_settings": _shop_public(row), "effective": await effective(db, shop.id) if warehouse else None}


async def configure_warehouse(db, payload):
    warehouse = await _warehouse(db, code=payload.warehouse_code, lock="update")
    row = await _one(db, StockWarehouseSettings, StockWarehouseSettings.warehouse_id == warehouse.id, "update")
    if payload.expected_revision != (row.revision if row else 0):
        raise SettingsError("stock_settings_changed")
    # Verify inherited combinations too, without acquiring Shop locks after Warehouse.
    overrides = (await db.scalars(select(StockShopSettings).join(OrderStockPolicy,
        OrderStockPolicy.shop_id == StockShopSettings.shop_id).where(OrderStockPolicy.warehouse_id == warehouse.id))).all()
    for shop in overrides:
        try:
            OperationalValues(**{**payload.values.model_dump(), **shop.overrides})
        except ValidationError:
            raise SettingsError("stock_settings_inherited_values_conflict", 422) from None
    if row is None:
        row = StockWarehouseSettings(warehouse_id=warehouse.id, revision=1)
        db.add(row)
    else:
        row.revision += 1
    row.values, row.processing_paused, row.updated_at = payload.values.model_dump(), payload.processing_paused, now()
    await db.flush()
    response = _warehouse_public(warehouse, row)
    await db.commit()
    return response


async def configure_shop(db, payload):
    shop = await _shop(db, payload.shop_code, lock="update")
    policy = await _one(db, OrderStockPolicy, OrderStockPolicy.shop_id == shop.id, "share")
    if policy is None:
        raise SettingsError("stock_settings_policy_required")
    warehouse = await _warehouse(db, identifier=policy.warehouse_id, lock="share")
    warehouse_row = await _one(db, StockWarehouseSettings, StockWarehouseSettings.warehouse_id == warehouse.id, "share")
    row = await _one(db, StockShopSettings, StockShopSettings.shop_id == shop.id, "update")
    if payload.expected_revision != (row.revision if row else 0) or payload.expected_warehouse_revision != (warehouse_row.revision if warehouse_row else 0):
        raise SettingsError("stock_settings_changed")
    try:
        OperationalValues(**{**_values(warehouse_row), **payload.overrides})
    except ValidationError:
        raise SettingsError("stock_settings_invalid_values", 422) from None
    fingerprint = None
    if payload.mode != "manual":
        # Imported lazily to keep the read-only collector independent of physical processing.
        from inventory_hub.services.order_collection import target_fingerprint
        collector = await _one(db, OrderCollectionSettings, OrderCollectionSettings.shop_id == shop.id)
        fingerprint = target_fingerprint(shop.code)
        if collector is None or fingerprint is None:
            raise SettingsError("stock_settings_collection_required")
        if collector.target_fingerprint != fingerprint or (row and row.target_fingerprint and row.target_fingerprint != fingerprint):
            raise SettingsError("stock_settings_target_changed")
        if payload.mode == "fulfill" and not payload.fulfillment_confirmed:
            raise SettingsError("stock_settings_fulfillment_confirmation_required", 422)
    previous = row.mode if row else "manual"
    at = now()
    if row is None:
        row = StockShopSettings(shop_id=shop.id, revision=1)
        db.add(row)
    else:
        row.revision += 1
    if payload.mode != "manual" and previous == "manual":
        row.automation_starts_at = at
    if payload.mode == "fulfill" and previous != "fulfill":
        row.issue_starts_at = at
    elif payload.mode != "fulfill":
        row.issue_starts_at = None
    row.overrides, row.mode, row.updated_at = payload.overrides, payload.mode, at
    row.authorized_policy_revision = policy.revision if payload.mode != "manual" else None
    if fingerprint:
        row.target_fingerprint = fingerprint
    await db.flush()
    response = await options(db, shop.code)
    await db.commit()
    return response
