"""Read-only history and lost-response recovery for the retired Zookee rename.

The former TOPEAK- -> ZOO- operation has no durable pending phase: its result,
local changes and audit were committed together. Existing result UUIDs remain
readable, but a missing result never authorizes a new rename or native-ID repair.
Shop publication recovery belongs to its original publication workflow.
"""
from uuid import UUID

from sqlalchemy import select

from inventory_hub.product_editor_models import ProductEditorAudit, ProductEditorSave
from inventory_hub.services import product_editor as editor

OLD, NEW = 'TOPEAK-', 'ZOO-'
RETIRED = 'code_rename_retired'
OPERATIONS = ('verified_code_reconciliation', 'verified_native_id_repair')


def _supplier(supplier):
    # History must remain readable even if the old prefix config was removed.
    if supplier != 'zookee':
        raise editor.EditorError('code_rename_configuration_invalid', 422)


def _hash(product_id):
    return editor._digest(['zookee-code-reconciliation', product_id])


def _result(saved):
    result = saved.result if saved else None
    if (not isinstance(result, dict) or not isinstance(result.get('product_id'), int)
            or isinstance(result['product_id'], bool)
            or saved.input_hash != _hash(result['product_id'])):
        raise editor.EditorError('code_rename_result_missing', 404)
    return result


async def preview(db, supplier):
    """List completed historical operations, never fresh migration candidates."""
    _supplier(supplier)
    rows = (await db.execute(select(ProductEditorSave)
        .join(ProductEditorAudit, ProductEditorAudit.request_id == ProductEditorSave.request_id)
        .where(ProductEditorAudit.after_data['operation'].astext.in_(OPERATIONS))
        .order_by(ProductEditorSave.created_at.desc(), ProductEditorAudit.id.desc()).limit(100))).scalars().all()
    results = []
    seen = set()
    for saved in rows:
        if saved.request_id in seen:
            continue
        try:
            result = _result(saved)
        except editor.EditorError:
            continue
        results.append({**result, 'completed_at': saved.created_at.isoformat()})
        seen.add(saved.request_id)
    return {'retired': True, 'reason': RETIRED, 'old_prefix': OLD, 'new_prefix': NEW,
            'items': [], 'results': results}


async def read_result(db, supplier, request_id):
    """Recover an original committed result without a remote call or mutation."""
    _supplier(supplier)
    saved = await db.get(ProductEditorSave, str(UUID(str(request_id))), populate_existing=True)
    return _result(saved)


async def reconcile_one(db, supplier, product_id, request_id):
    """Keep old successful UUID retries readable; reject every fresh request."""
    _supplier(supplier)
    saved = await db.get(ProductEditorSave, str(UUID(str(request_id))), populate_existing=True)
    if saved is None:
        raise editor.EditorError(RETIRED, 410)
    if saved.input_hash != _hash(product_id):
        raise editor.EditorError('product_editor_request_reused')
    return _result(saved)


async def reconcile_batch(db, supplier, rows):
    """Older clients may recover completed rows, but cannot start another batch."""
    _supplier(supplier)
    results = []
    for row in rows:
        try:
            result = await reconcile_one(db, supplier, row.product_id, row.request_id)
        except editor.EditorError as error:
            if error.code != RETIRED:
                raise
            result = {'product_id': row.product_id, 'request_id': str(row.request_id),
                      'status': 'failed', 'error': RETIRED}
        results.append(result)
    return {'retired': True, 'results': results}
