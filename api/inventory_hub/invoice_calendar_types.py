"""Read-only due-date calendar inputs; issue-date filters retain their meaning."""
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class InvoiceCalendarQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    range_from: date
    range_to: date
    direction: Literal["purchase", "sale"] = "purchase"
    supplier_code: str | None = Field(default=None, max_length=100)
    f_supplier: str | None = Field(default=None, max_length=255)
    f_invoice_number: str | None = Field(default=None, max_length=255)
    date_from: date | None = None
    date_to: date | None = None
    due_from: date | None = None
    due_to: date | None = None
    f_amount_min: Decimal | None = None
    f_amount_max: Decimal | None = None
    f_currency: str | None = Field(default=None, max_length=3)
    payment_status: Literal["unknown", "unpaid", "partial", "paid"] | None = None
    receiving_status: str | None = Field(default=None, max_length=32)
    is_overdue: bool | None = None

    @field_validator("f_amount_min", "f_amount_max")
    @classmethod
    def finite_amount(cls, value):
        if value is not None and (not value.is_finite() or abs(value) > Decimal("999999999999.99")):
            raise ValueError("Use a finite amount in the invoice range")
        return value

    @model_validator(mode="after")
    def bounded_period(self):
        # Inclusive 366 days covers a complete leap year.
        if not 0 <= (self.range_to - self.range_from).days < 366:
            raise ValueError("Choose a calendar period of at most 366 days")
        for first, last in ((self.date_from, self.date_to), (self.due_from, self.due_to)):
            if first and last and last < first:
                raise ValueError("The end date precedes the start date")
        if self.f_amount_min is not None and self.f_amount_max is not None and self.f_amount_max < self.f_amount_min:
            raise ValueError("The maximum amount is below the minimum")
        return self
