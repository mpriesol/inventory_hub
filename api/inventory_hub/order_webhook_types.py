"""Admin intent only; no order lines or stock values are accepted."""
from urllib.parse import urlsplit
from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator
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
