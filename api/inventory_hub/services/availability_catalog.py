"""Shared dictionaries and manual desired availability; never physical stock."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from sqlalchemy import func, select, text
from inventory_hub.db_models import Product, ShopWarehouse, Warehouse
from inventory_hub.db_models_ext import ReceivingSession, StockBalance
from inventory_hub.product_editor_models import ProductEditorAudit, ProductEditorOverride, ProductEditorSave
from inventory_hub.setting_catalog_models import AvailabilityValue, SettingCatalogRequest
from inventory_hub.setting_catalog_types import AvailabilityOverrideChange
from inventory_hub.services.canonical_products import current_product_condition
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK


class CatalogError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def now():
    return datetime.now(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


async def lock(db, scope):
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': int(digest(scope)[:15], 16)})


def availability_dto(row):
    return {key: getattr(row, key) for key in ('id', 'label', 'is_active', 'sort_order', 'revision')}


async def list_values(db, include_inactive=False):
    query = select(AvailabilityValue).order_by(AvailabilityValue.sort_order, AvailabilityValue.label, AvailabilityValue.id)
    if not include_inactive:
        query = query.where(AvailabilityValue.is_active.is_(True))
    return {'items': [availability_dto(row) for row in await db.scalars(query)]}


async def active_labels(db):
    return [row['label'] for row in (await list_values(db))['items']]


async def validate_labels(db, labels):
    values = {value for value in labels if value is not None}
    if values and values - set(await active_labels(db)):
        raise CatalogError('availability_value_missing', 422)


async def _request(db, payload, scope):
    identifier = str(payload.request_id)
    hashed = digest({'scope': scope, 'data': payload.model_dump(mode='json')})
    await lock(db, ['settings-catalogs', identifier])
    old = await db.get(SettingCatalogRequest, identifier)
    if old and old.input_hash != hashed:
        raise CatalogError('catalog_request_reused')
    return identifier, hashed, old.result if old else None


async def _saved(db, identifier, hashed, result, actor):
    db.add(SettingCatalogRequest(request_id=identifier, input_hash=hashed, result=result, actor=actor))
    await db.commit()
    return result


async def save_value(db, payload, *, actor):
    identifier, hashed, old = await _request(db, payload, 'availability')
    if old is not None:
        return old
    await lock(db, 'availability-dictionary')
    row = await db.get(AvailabilityValue, payload.id, populate_existing=True) if payload.id else None
    if payload.id and row is None:
        raise CatalogError('catalog_value_missing', 404)
    if payload.expected_revision != (row.revision if row else 0):
        raise CatalogError('catalog_changed')
    duplicate = await db.scalar(select(AvailabilityValue.id).where(
        func.lower(func.trim(AvailabilityValue.label)) == payload.label.lower()))
    if duplicate is not None and (row is None or duplicate != row.id):
        raise CatalogError('catalog_duplicate')
    if row is None:
        row = AvailabilityValue(label=payload.label, is_active=payload.is_active,
            sort_order=payload.sort_order, revision=1, updated_at=now())
        db.add(row)
    else:
        # Historical explicit intent keeps its exact label after a rename.
        row.label, row.is_active, row.sort_order = payload.label, payload.is_active, payload.sort_order
        row.revision += 1
        row.updated_at = now()
    await db.flush()
    return await _saved(db, identifier, hashed, availability_dto(row), actor)


def warehouse_dto(row):
    return {**{key: getattr(row, key) for key in ('id', 'code', 'name', 'address', 'is_active', 'is_default')},
        'updated_at': row.updated_at.isoformat()}


async def list_warehouses(db, include_inactive=False):
    query = select(Warehouse).order_by(Warehouse.is_default.desc(), Warehouse.name, Warehouse.id)
    if not include_inactive:
        query = query.where(Warehouse.is_active.is_(True))
    return {'items': [warehouse_dto(row) for row in await db.scalars(query)]}


async def save_warehouse(db, payload, *, actor):
    identifier, hashed, old = await _request(db, payload, 'warehouse')
    if old is not None:
        return old
    await lock(db, 'warehouse-dictionary')
    rows = list(await db.scalars(select(Warehouse).order_by(Warehouse.id).with_for_update()
        .execution_options(populate_existing=True)))
    row = next((item for item in rows if item.code == payload.code), None)
    if row and (payload.expected_updated_at is None or row.updated_at != payload.expected_updated_at):
        raise CatalogError('catalog_changed')
    if row is None and payload.expected_updated_at is not None:
        raise CatalogError('catalog_value_missing', 404)
    if row is None and not payload.is_active and not any(item.is_active for item in rows):
        raise CatalogError('warehouse_default_inactive', 422)
    if payload.is_default and not payload.is_active:
        raise CatalogError('warehouse_default_inactive', 422)
    if row and not payload.is_active:
        if row.is_default:
            raise CatalogError('warehouse_default_required')
        busy = await db.scalar(select(StockBalance.id).where(StockBalance.warehouse_id == row.id,
            (StockBalance.qty_on_hand != 0) | (StockBalance.qty_reserved != 0) | (StockBalance.qty_quarantined != 0)).limit(1))
        linked = await db.scalar(select(ShopWarehouse.id).where(ShopWarehouse.warehouse_id == row.id,
            ShopWarehouse.is_active.is_(True)).limit(1))
        open_receipt = await db.scalar(select(ReceivingSession.id).where(ReceivingSession.warehouse_id == row.id,
            ReceivingSession.status.in_(['new', 'in_progress', 'paused']),
            ~ReceivingSession.session_data.has_key('receiving_archive')).limit(1))
        open_batch = await db.scalar(text("SELECT id FROM mixed_receiving_batches WHERE warehouse_id=:id AND status='open' AND archived_at IS NULL LIMIT 1"), {'id': row.id})
        if busy is not None or linked is not None or open_receipt is not None or open_batch is not None:
            raise CatalogError('warehouse_in_use')
    if row and row.is_default and not payload.is_default:
        raise CatalogError('warehouse_default_required')
    if payload.is_default:
        for other in rows:
            if other.id != (row.id if row else None) and other.is_default:
                other.is_default = False
                other.updated_at = now()
        await db.flush()
    if row is None:
        row = Warehouse(code=payload.code, name=payload.name, is_active=payload.is_active,
            is_default=payload.is_default or not any(item.is_active for item in rows), address=payload.address)
        db.add(row)
    else:
        row.name, row.address = payload.name, payload.address
        row.is_active, row.is_default = payload.is_active, payload.is_default
    row.updated_at = now()
    await db.flush()
    await db.refresh(row)  # The warehouse update timestamp trigger is authoritative.
    return await _saved(db, identifier, hashed, warehouse_dto(row), actor)


async def get_product_overrides(db, product_ids):
    ids = list(set(product_ids))
    if not ids:
        return {}
    # Revision-only entries matter: other card edits also participate in CAS.
    return {row.product_id: {**deepcopy(row.data.get('availability', {})), 'revision': row.revision}
        for row in await db.scalars(select(ProductEditorOverride).where(ProductEditorOverride.product_id.in_(ids)))}


async def apply_product_overrides(db, changes, request_id, actor):
    """Part of caller transaction; no commit, quantity mutation or remote call.

    expected_revision is the reviewed card revision. UUID replay retains the
    exact payload. Identity and product locks follow the existing editor order.
    """
    changes = [AvailabilityOverrideChange.model_validate(change).model_dump(exclude_unset=True) for change in changes]
    ids = [change['product_id'] for change in changes]
    if len(ids) != len(set(ids)):
        raise CatalogError('catalog_duplicate_product', 422)
    identifier = str(request_id)
    hashed = digest({'scope': 'availability-override', 'changes': changes})
    await lock(db, ['product-editor', identifier])
    prior = await db.get(ProductEditorSave, identifier)
    if prior:
        if prior.input_hash != hashed:
            raise CatalogError('catalog_request_reused')
        return prior.result
    await db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': IDENTITY_WRITE_LOCK})
    found = list(await db.scalars(select(Product.id).where(Product.id.in_(ids), current_product_condition())
        .order_by(Product.id).with_for_update()))
    if len(found) != len(ids):
        raise CatalogError('product_editor_product_missing', 404)
    existing = {row.product_id: row for row in await db.scalars(select(ProductEditorOverride)
        .where(ProductEditorOverride.product_id.in_(ids)).execution_options(populate_existing=True))}
    for change in changes:
        revision = existing[change['product_id']].revision if change['product_id'] in existing else 0
        if revision != change['expected_revision']:
            raise CatalogError('catalog_changed')
    await validate_labels(db, [change.get('label') for change in changes if not change.get('clear')
        and change.get('label') != (existing[change['product_id']].data.get('availability', {}).get('label')
            if change['product_id'] in existing else None)])
    request = ProductEditorSave(request_id=identifier, input_hash=hashed, result={})
    db.add(request)
    await db.flush()
    results = []
    for change in changes:
        pid = change['product_id']
        row = existing.get(pid)
        before = deepcopy(row.data if row else {})
        after = deepcopy(before)
        value = deepcopy(after.get('availability', {}))
        if change.get('clear'):
            after.pop('availability', None)
        else:
            for key in ('label', 'visible'):
                if key in change:
                    if change[key] is None:
                        value.pop(key, None)
                    else:
                        value[key] = change[key]
            if 'label' in value or 'visible' in value:
                value.update(source='manual', actor=actor, updated_at=now().isoformat())
                after['availability'] = value
            else:
                after.pop('availability', None)
        revision = (row.revision if row else 0) + 1
        if row is None:
            row = ProductEditorOverride(product_id=pid, revision=revision, data=after, updated_at=now())
            db.add(row)
        else:
            row.revision, row.data, row.updated_at = revision, after, now()
        db.add(ProductEditorAudit(request_id=identifier, product_id=pid, revision=revision,
            before_data={'overrides': before}, after_data={'overrides': after}))
        results.append({'product_id': pid, 'revision': revision, 'availability_override': after.get('availability')})
    request.result = {'request_id': identifier, 'results': results, 'external_write_enabled': False}
    await db.flush()
    return request.result
