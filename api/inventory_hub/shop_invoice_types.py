"""Bounded operator-selected reads of invoices already issued by a shop."""
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class ShopInvoiceSyncRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    date_from: date
    date_to: date
    page: StrictInt = Field(default=1, ge=1, le=100_000)
    offset: StrictInt = Field(default=0, ge=0, le=99)
    page_token: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='after')
    def bounded_period(self):
        if not 0 <= (self.date_to - self.date_from).days <= 366:
            raise ValueError('invoice_sync_date_range_invalid')
        if self.offset and self.page_token is None:
            raise ValueError('invoice_sync_page_token_required')
        return self
