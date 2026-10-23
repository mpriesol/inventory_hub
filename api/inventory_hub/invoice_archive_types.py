"""Archive inputs describe already issued source documents, never new invoices."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator


class ArchiveInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


def exact_decimal(value, scale=2, *, positive=False):
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError('Use exact decimal strings')
    try:
        number = Decimal(str(value))
        if (not number.is_finite() or abs(number) >= Decimal('1000000000000')
                or number != number.quantize(Decimal(1).scaleb(-scale))
                or positive and number <= 0):
            raise ValueError('Invalid amount or precision')
        return number
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('Invalid amount or precision') from None


class ArchiveMetadata(ArchiveInput):
    invoice_number: StrictStr | None = Field(default=None, min_length=1, max_length=100)
    invoice_date: date | None = None
    due_date: date | None = None
    currency: StrictStr | None = Field(default=None, pattern=r'^[A-Z]{3}$')
    total_amount: Decimal | None = None
    total_without_vat: Decimal | None = None
    vat_amount: Decimal | None = None
    total_with_vat: Decimal | None = None
    document_type: Literal['invoice', 'credit_note', 'receipt'] = 'invoice'
    source_payment_status: StrictStr | None = Field(default=None, max_length=100)
    source_paid_amount: Decimal | None = None
    source_paid_at: datetime | None = None
    source_paid_date: date | None = None
    parse_error: StrictStr | None = Field(default=None, max_length=255)

    @field_validator('total_amount', 'total_without_vat', 'vat_amount', 'total_with_vat', 'source_paid_amount', mode='before')
    @classmethod
    def money(cls, value):
        return None if value is None else exact_decimal(value)

    @field_validator('invoice_number')
    @classmethod
    def text(cls, value):
        if value is not None and (value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError('Use exact source text')
        return value


class ArchiveLine(ArchiveInput):
    line_number: StrictInt = Field(gt=0)
    source_line_key: StrictStr | None = Field(default=None, max_length=255)
    ean: StrictStr | None = Field(default=None, max_length=255)
    barcodes: list[StrictStr] = Field(default_factory=list, max_length=100)
    supplier_sku: StrictStr | None = Field(default=None, max_length=100)
    product_name: StrictStr | None = Field(default=None, max_length=10000)
    quantity: Decimal
    unit: StrictStr | None = Field(default=None, max_length=20)
    unit_price: Decimal | None = None
    total_price: Decimal | None = None
    vat_rate: Decimal | None = None
    unit_price_with_vat: Decimal | None = None
    total_price_with_vat: Decimal | None = None
    matched_product_id: StrictInt | None = Field(default=None, gt=0)
    matched_supplier_product_id: StrictInt | None = Field(default=None, gt=0)
    match_evidence: dict | None = None

    @field_validator('quantity', mode='before')
    @classmethod
    def quantity_value(cls, value):
        number = exact_decimal(value, 3)
        if abs(number) >= Decimal('1000000000') or number == 0:
            raise ValueError('Invalid quantity')
        return number

    @field_validator('unit_price', 'unit_price_with_vat', mode='before')
    @classmethod
    def unit_money(cls, value):
        number = None if value is None else exact_decimal(value, 4)
        if number is not None and abs(number) >= Decimal('100000000'):
            raise ValueError('Unit price out of range')
        return number

    @field_validator('total_price', 'total_price_with_vat', mode='before')
    @classmethod
    def line_money(cls, value):
        return None if value is None else exact_decimal(value)

    @field_validator('vat_rate', mode='before')
    @classmethod
    def tax(cls, value):
        number = None if value is None else exact_decimal(value)
        if number is not None and not 0 <= number <= 100:
            raise ValueError('Invalid source VAT')
        return number


class InvoiceReconcileInput(ArchiveInput):
    supplier_code: StrictStr = Field(pattern=r'^[a-zA-Z0-9_-]{1,50}$')
    after_key: StrictStr | None = Field(default=None, max_length=500)
    limit: StrictInt = Field(default=100, ge=1, le=200)
