"""Bounded request-local source reads; every card keeps its own transaction.

Only native observations are shared. Local owners, history and publication
guards are reloaded under the identity lock for each existing card workflow.
No observation is accepted from a client or reused by another HTTP request.
"""
from dataclasses import dataclass
from time import monotonic
from uuid import uuid4

from inventory_hub.shared_stock_sale_models import SharedStockSalePreview
from inventory_hub.services import product_links as links, shared_stock_sales as sales


MAX_OBSERVATION_SECONDS = 60


def _native_target(sale):
    return {'code': sale['code'], 'parent_code': sale['parent_code'], 'is_variant': sale['is_variant'],
            'parent_id': str(sale['native_product_id']),
            'external_id': str(sale['native_variant_id'] if sale['is_variant'] else sale['native_product_id'])}


def _previous(pin):
    return {'id': pin['mapping_id'], 'is_variant': pin['native_variant_id'] is not None,
            'external_product_id': str(pin['native_product_id']),
            'external_variant_id': str(pin['native_variant_id']) if pin['native_variant_id'] is not None else None}


def _previous_identity(row):
    return {key: row[key] for key in ('id', 'is_variant', 'external_product_id', 'external_variant_id')}


@dataclass(frozen=True)
class _Observation:
    shop_code: str
    started: float
    started_at: str
    finished_at: str
    read_id: str
    scope_hash: str
    observed: dict
    previous: dict
    deletion: dict

    def require_fresh(self, shop_code):
        if shop_code != self.shop_code:
            raise links.LinkError('shared_stock_sale_batch_scope_changed')
        if monotonic() - self.started > MAX_OBSERVATION_SECONDS:
            raise links.LinkError('shared_stock_sale_batch_observation_expired')

    def proof(self):
        return {'id': self.read_id, 'scope_hash': self.scope_hash, 'fingerprint': self.observed['fingerprint'],
                'started_at': self.started_at, 'finished_at': self.finished_at,
                'max_age_seconds': MAX_OBSERVATION_SECONDS,
                'shared_native_requests': self.observed['requests'] + self.deletion['requests']}

    def absence(self, local):
        self.require_fresh(self.shop_code)
        previous = local.get('replaced_mappings', [])
        if not previous:
            return None
        for row in previous:
            if self.previous.get(row['id']) != _previous_identity(row):
                raise links.LinkError('shared_stock_sale_replacement_changed')
        identifiers = [row['id'] for row in previous]
        if set(identifiers) & set(self.deletion['existing_mapping_ids']):
            raise links.LinkError('shared_stock_sale_original_still_exists')
        return {'mapping_ids': identifiers, 'fingerprint': self.observed['fingerprint'],
                'active_and_archived': True, 'checked_at': self.finished_at,
                'requests': self.deletion['requests'], 'shared_read_id': self.read_id}


async def _read(shop_code, targets, previous, fingerprint=None):
    started, started_at = monotonic(), links.now().isoformat()
    observed = await links._source_call(links.source.discover(shop_code,
        sorted({row['code'] for row in targets}), fingerprint, max_requests=links.MAX_REQUESTS))
    deletion = {'requests': 0, 'existing_mapping_ids': []}
    if previous:
        deletion = await links._verify_replacement_absence(shop_code, previous, observed['fingerprint'],
            max_requests=links.MAX_REQUESTS - observed['requests'], collect_conflicts=True)
    result = _Observation(shop_code, started, started_at, links.now().isoformat(), str(uuid4()),
        links.digest({'shop_code': shop_code, 'targets': sorted(targets, key=links.source._key),
                      'previous': sorted((_previous_identity(row) for row in previous), key=lambda row: row['id'])}),
        observed, {row['id']: _previous_identity(row) for row in previous}, deletion)
    result.require_fresh(shop_code)
    return result


def _response(identifiers):
    return {'status': 'completed', 'items': [{'id': str(identifier), 'outcome': 'not_attempted'}
                                          for identifier in identifiers]}


def _error(error):
    if not isinstance(error, links.LinkError):
        return {'code': 'shared_stock_sale_batch_outcome_unknown', 'status': 500}
    result = {'code': error.code, 'status': error.status}
    if error.known_unapplied:
        result['known_unapplied'] = True
    if error.retry_after is not None:
        result['retry_after'] = error.retry_after
    return result


async def _stop(db, result, index, error, *, stage, fence=None):
    result.update(status='stopped', stopped_at=result['items'][index]['id'], failure_stage=stage)
    if stage == 'source_read':
        # A concurrent individual apply may already have won. The common read
        # still failed, so a recovered first result must not restart the tail.
        result['batch_error'] = _error(error)
    if getattr(error, 'retry_after', None) is not None:
        result['retry_after'] = error.retry_after
    if fence is not None and isinstance(error, links.LinkError) and error.code not in (
            'shared_stock_sale_not_found', 'shared_stock_sale_preview_changed'):
        try:
            recovered = await sales.fence_failed_apply(db, str(fence.id), fence, error)
            result['items'][index].update(outcome='replayed', data=recovered)
            return result
        except Exception as fenced_error:
            error = fenced_error
    try:
        await db.rollback()
    except Exception:
        # Unknown transaction/commit failures never gain a known-unapplied
        # claim. Individual GET recovery is authoritative after reconnecting.
        error = RuntimeError('Rollback outcome unavailable')
    result['items'][index].update(outcome='failed' if isinstance(error, links.LinkError) else 'outcome_unknown',
                                   error=_error(error))
    return result


async def preview(db, payload, *, actor=None):
    items = payload.items
    result = _response(item.request_id for item in items)
    existing, pending = {}, []
    for index, item in enumerate(items):
        try:
            row = await db.get(SharedStockSalePreview, str(item.request_id))
            if row is not None:
                _, request_hash = sales.request_document(item)
                if row.request_hash != request_hash:
                    raise links.LinkError('shared_stock_sale_request_changed')
                existing[index] = sales.public(row)
            else:
                pending.append(index)
        except Exception as error:
            return await _stop(db, result, index, error, stage='preflight')
    observation = None
    if pending:
        raw = [sales.request_document(items[index])[0] for index in pending]
        targets = [_native_target(sale) for item in raw for sale in item['sales']]
        previous = [_previous(sale['replace_deleted_mapping']) for item in raw for sale in item['sales']
                    if sale.get('replace_deleted_mapping')]
        try:
            await links._shop(db, items[0].shop_code)
            await db.commit()  # Never hold a business transaction across GETs.
            observation = await _read(items[0].shop_code, targets, previous)
        except Exception as error:
            for index in range(pending[0]):
                result['items'][index].update(outcome='replayed', data=existing[index])
            return await _stop(db, result, pending[0], error, stage='source_read')
    for index, item in enumerate(items):
        try:
            data = (existing[index] if index in existing else
                    await sales.preview(db, item, actor=actor, _observation=observation))
            outcome = 'prepared' if index not in existing and data['status'] == 'prepared' else 'replayed'
            result['items'][index].update(outcome=outcome, data=data)
            await db.commit()  # Also release locks after a concurrent replay.
        except Exception as error:
            return await _stop(db, result, index, error, stage='item')
    if observation is not None:
        result['source_read'] = observation.proof()
    return result


async def apply(db, payload, *, actor=None):
    items = payload.items
    result = _response(item.id for item in items)
    rows, pending, targets, previous = {}, [], [], []
    products, codes, identities, fingerprints, mapping_ids = set(), set(), set(), set(), set()
    leaf_count = 0
    for index, item in enumerate(items):
        try:
            row = await db.get(SharedStockSalePreview, str(item.id))
            needed = sales._check(row, item, expired=True)
            if row.document['shop_code'] != payload.shop_code or row.product_id in products:
                raise links.LinkError('shared_stock_sale_batch_scope_changed', 422)
            products.add(row.product_id)
            group = [entry['remote'] for entry in row.document['selected']]
            leaf_count += len(group)
            for remote in group:
                code, key = remote['code'].casefold(), links.remote_key(remote)
                if code in codes or key in identities:
                    raise links.LinkError('shared_stock_sale_batch_scope_changed', 422)
                codes.add(code)
                identities.add(key)
            if leaf_count > 100:
                raise links.LinkError('shared_stock_sale_batch_scope_changed', 422)
            rows[index] = row
            if needed:
                sales._check(row, item)
                fingerprints.add(row.document['fingerprint'])
                if len(fingerprints) != 1:
                    raise links.LinkError('source_connection_changed')
                pending.append(index)
                targets.extend(links.remote_identity(remote) for remote in group)
                for original in row.document['local'].get('replaced_mappings', []):
                    if original['id'] in mapping_ids:
                        raise links.LinkError('shared_stock_sale_batch_scope_changed', 422)
                    mapping_ids.add(original['id'])
                    previous.append(original)
        except Exception as error:
            # Scope/hash problems do not authorize expiring another operation.
            fence = item if isinstance(error, links.LinkError) and error.code in (
                'shared_stock_sale_preview_expired', 'source_connection_changed') else None
            return await _stop(db, result, index, error, stage='preflight', fence=fence)
    observation = None
    if pending:
        try:
            await links._shop(db, payload.shop_code)
            await db.commit()
            observation = await _read(payload.shop_code, targets, previous, next(iter(fingerprints)))
        except Exception as error:
            for index in range(pending[0]):
                result['items'][index].update(outcome='replayed', data=sales.public(rows[index]))
            return await _stop(db, result, pending[0], error, stage='source_read', fence=items[pending[0]])
    for index, item in enumerate(items):
        try:
            if index not in pending:
                data = sales.public(rows[index])
            else:
                data = await sales.apply(db, str(item.id), item, actor=actor, _observation=observation)
            result['items'][index].update(outcome='completed' if index in pending else 'replayed', data=data)
            await db.commit()
        except Exception as error:
            return await _stop(db, result, index, error, stage='item')
    if observation is not None:
        result['source_read'] = observation.proof()
    return result
