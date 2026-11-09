"""Admin intent only; no order lines or stock values are accepted."""
from urllib.parse import urlsplit
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator
from inventory_hub.order_collection_types import CollectionConfirmation


class WebhookConfigure(CollectionConfirmation):
    enabled: StrictBool
    public_base_url: StrictStr = Field(min_length=1, max_length=500)
    catchup_interval_seconds: StrictInt = Field(default=3600, ge=300, le=86400)

    @field_validator('public_base_url')
    @classmethod
    def public_url(cls, value):
        try:
            parsed = urlsplit(value)
            if (value != value.strip() or any(ord(c) < 33 or ord(c) == 127 for c in value)
                    or parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or parsed.port not in (None, 443)
                    or '//' in parsed.path or '..' in parsed.path or '%' in parsed.path):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('A public HTTPS Hub API base URL is required') from None
        return value.rstrip('/')


class WebhookResolveRegistration(CollectionConfirmation):
    event: Literal['create', 'update', 'delete']
    intent_id: StrictStr = Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
    remote_request_finished_confirmed: Literal[True]
    note: StrictStr = Field(min_length=10, max_length=2000)

    @field_validator('remote_request_finished_confirmed', mode='before')
    @classmethod
    def finished(cls, value):
        if value is not True:
            raise ValueError('Explicit settled-request confirmation required')
        return value

    @field_validator('note')
    @classmethod
    def evidence(cls, value):
        if len(value.strip()) < 10 or any(ord(char) < 32 and char not in '\n\t' for char in value):
            raise ValueError('Describe the external settlement evidence')
        return value.strip()


class NotificationResolutionPreview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    shop_code: StrictStr = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,49}$')
    order_number: StrictStr = Field(min_length=1, max_length=100)
    action: Literal['acknowledge_absent_delete', 'reopen']
    note: StrictStr = Field(min_length=10, max_length=2000)

    @field_validator('order_number')
    @classmethod
    def exact_order(cls, value):
        if value != value.strip() or ';' in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('An exact order number is required')
        try:
            value.encode('utf-8')
        except UnicodeError:
            raise ValueError('An exact order number is required') from None
        return value

    @field_validator('note')
    @classmethod
    def evidence(cls, value):
        if len(value.strip()) < 10 or any((ord(char) < 32 and char not in '\n\t') or ord(char) == 127 for char in value):
            raise ValueError('Describe why this notification is being resolved or reopened')
        return value.strip()


class NotificationResolutionApply(BaseModel):
    model_config = ConfigDict(extra='forbid')
    shop_code: StrictStr = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,49}$')
    preview_hash: StrictStr = Field(pattern=r'^[0-9a-f]{64}$')
    confirmed: Literal[True]

    @field_validator('confirmed', mode='before')
    @classmethod
    def confirmation(cls, value):
        if value is not True:
            raise ValueError('Explicit confirmation required')
        return value
