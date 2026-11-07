"""Audited campaign lifecycle controls; no job, identity or remote mutations."""
from datetime import timezone
from hashlib import sha256
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text

from inventory_hub.services import atomer_catalog_engine as engine
from inventory_hub.services.atomer_review_targets import ReviewError
from inventory_hub.services.product_identity import IDENTITY_WRITE_LOCK


Action = Literal['pause', 'resume']
CampaignID = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
REVIEW_LOCK = 691433138


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    request_id: UUID
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    expected_updated_at: AwareDatetime
    reason: Annotated[str, Field(min_length=10, max_length=1000)]
    confirmed: Literal[True]

    @field_validator('confirmed', mode='before')
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError('explicit_confirmation_required')
        return value

    @field_validator('reason')
    @classmethod
    def plain_reason(cls, value):
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('plain_reason_required')
        return value

    @field_validator('expected_updated_at')
    @classmethod
    def utc_timestamp(cls, value):
        return value.astimezone(timezone.utc)


def _public(row):
    return dict(campaign_id=row['id'], state=row['state'], operator_paused=row['operator_paused'],
                revision=row['operator_control_revision'], previous_state=row['operator_pause_previous_state'],
                updated_at=row['updated_at'].astimezone(timezone.utc).isoformat(),
                next_at=row['next_at'].astimezone(timezone.utc).isoformat(), failures=row['failures'],
                run_id=row['run_id'], checked_run_id=row['checked_run_id'])


async def get(db, campaign_id):
    row = await engine._one(db, 'SELECT * FROM atomer_catalog_campaigns WHERE id=:c', c=campaign_id)
    if not row:
        raise ReviewError('catalog_campaign_not_found', 404)
    return _public(row)


async def receipt(db, request_id):
    row = await engine._one(db, 'SELECT result FROM atomer_catalog_controls WHERE request_id=:id', id=str(request_id))
    if not row:
        raise ReviewError('catalog_control_not_found', 404)
    return row['result']


async def _replay(db, request_id, request_hash):
    row = await engine._one(db, 'SELECT request_hash,result FROM atomer_catalog_controls WHERE request_id=:id',
                            id=str(request_id))
    if row and row['request_hash'] != request_hash:
        raise ReviewError('catalog_control_request_conflict')
    return row['result'] if row else None


async def _lock(db):
    # Transaction and session advisory locks share the same namespace. The
    # remote worker retains WORKER_LOCK across intent commits and bounded HTTP.
    # The review mutex also fences registration and the local source-card lane.
    # Keep all three until the state and its receipt are committed together.
    for key, code in ((engine.WORKER_LOCK, 'catalog_worker_busy'),
                      (REVIEW_LOCK, 'catalog_review_busy'),
                      (IDENTITY_WRITE_LOCK, 'catalog_identity_busy')):
        if not await db.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': key}):
            raise ReviewError(code)


async def change(db, campaign_id, action: Action, payload: ControlRequest, actor):
    """A successful response is a committed quiescence/control receipt."""
    if action not in ('pause', 'resume'):
        raise ReviewError('catalog_control_action_invalid', 422)
    if not actor or not isinstance(actor, str) or len(actor) > 200:
        raise ReviewError('operator_required', 403)
    request = dict(campaign_id=campaign_id, action=action, **payload.model_dump(mode='json'))
    request_hash = sha256(json.dumps(request, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    try:
        saved = await _replay(db, payload.request_id, request_hash)
        if saved:
            await db.commit()
            return saved
        await _lock(db)
        saved = await _replay(db, payload.request_id, request_hash)
        if saved:
            await db.commit()
            return saved
        current = await engine._one(db, 'SELECT * FROM atomer_catalog_campaigns WHERE id=:c FOR UPDATE', c=campaign_id)
        if not current:
            raise ReviewError('catalog_campaign_not_found', 404)
        if (current['operator_control_revision'] != payload.expected_revision or
                current['updated_at'] != payload.expected_updated_at):
            raise ReviewError('catalog_control_changed')
        if action == 'pause':
            if current['operator_paused']:
                raise ReviewError('catalog_already_operator_paused')
            if current['state'] not in ('running', 'waiting', 'paused'):
                raise ReviewError('catalog_campaign_not_runnable')
            state, previous = 'paused', current['state']
        else:
            if not current['operator_paused']:
                raise ReviewError('catalog_not_operator_paused')
            state, previous = current['operator_pause_previous_state'], None
            if state in engine.ACTIVE and await engine._one(db, """SELECT id FROM atomer_catalog_campaigns
                    WHERE id<>:c AND state IN ('running','waiting')""", c=campaign_id):
                raise ReviewError('catalog_other_campaign_active')
        before = _public(current)
        updated = await engine._one(db, """UPDATE atomer_catalog_campaigns
            SET state=:state,operator_paused=:paused,operator_pause_previous_state=:previous,
                operator_control_revision=operator_control_revision+1,updated_at=clock_timestamp()
            WHERE id=:c RETURNING *""", c=campaign_id, state=state, paused=action == 'pause', previous=previous)
        counts = await engine._rows(db, 'SELECT stage,count(*) AS amount FROM atomer_catalog_jobs WHERE campaign=:c GROUP BY stage',
                                    c=campaign_id)
        result = dict(status='completed', request_id=str(payload.request_id), request_hash=request_hash,
                      campaign_id=campaign_id, action=action, actor=actor, reason=payload.reason,
                      before=before, after=_public(updated), jobs_unchanged=True,
                      job_stages={row['stage']: row['amount'] for row in counts})
        await engine._exec(db, """INSERT INTO atomer_catalog_controls
            (request_id,campaign,request_hash,action,actor,request,result)
            VALUES (:id,:c,:hash,:action,:actor,CAST(:request AS jsonb),CAST(:result AS jsonb))""",
            id=str(payload.request_id), c=campaign_id, hash=request_hash, action=action, actor=actor,
            request=engine._json(request), result=engine._json(result))
        await db.commit()
        return result
    except Exception:
        await db.rollback()
        raise
