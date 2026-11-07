"""An operator names every sale leaf and explicitly confirms its base pieces."""
from typing import Literal
from uuid import UUID
from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator
from inventory_hub.product_link_types import LinkInput, exact_code


class DeletedSaleMapping(LinkInput):
    """Pin the earlier native identity; absence is proved afresh by the server."""
    mapping_id: StrictInt = Field(gt=0)
    native_product_id: StrictInt = Field(gt=0, le=9007199254740991)
    native_variant_id: StrictInt | None = Field(default=None, gt=0, le=9007199254740991)


class SharedStockSaleTarget(LinkInput):
    code: StrictStr
    parent_code: StrictStr
    is_variant: StrictBool
    native_product_id: StrictInt = Field(gt=0, le=9007199254740991)
    native_variant_id: StrictInt | None = Field(default=None, gt=0, le=9007199254740991)
    sale_to_stock_factor: StrictInt = Field(ge=1, le=1000000)
    replace_deleted_mapping: DeletedSaleMapping | None = None

    @field_validator('code', 'parent_code')
    @classmethod
    def code_value(cls, value):
        return exact_code(value)

    @model_validator(mode='after')
    def shape(self):
        if self.is_variant != (self.native_variant_id is not None):
            raise ValueError('Supply the exact native identity for the selected leaf kind')
        if not self.is_variant and self.code != self.parent_code:
            raise ValueError('A standalone product is its own parent')
        return self


class SharedStockSalePreviewRequest(LinkInput):
    request_id: UUID
    shop_code: StrictStr = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,49}$')
    product_id: StrictInt = Field(gt=0)
    sales: list[SharedStockSaleTarget] = Field(min_length=1, max_length=50)
    identity_evidence: StrictStr = Field(min_length=10, max_length=2000)
    same_physical_item_confirmed: Literal[True]
    base_piece_unit_confirmed: Literal[True]

    @field_validator('same_physical_item_confirmed', 'base_piece_unit_confirmed', mode='before')
    @classmethod
    def confirmation(cls, value):
        if value is not True:
            raise ValueError('Explicit confirmation is required')
        return value

    @field_validator('identity_evidence')
    @classmethod
    def evidence(cls, value):
        if value != value.strip() or not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError('Describe the verified physical item and the source of its piece unit')
        return value

    @model_validator(mode='after')
    def distinct(self):
        codes = [row.code.casefold() for row in self.sales]
        identities = [(row.is_variant, row.native_variant_id if row.is_variant else row.native_product_id)
                      for row in self.sales]
        if len(set(codes)) != len(codes) or len(set(identities)) != len(identities):
            raise ValueError('Select each sale leaf once')
        replacements = [row.replace_deleted_mapping for row in self.sales if row.replace_deleted_mapping is not None]
        if len(replacements) > 1:
            raise ValueError('Only the single ordinary mapping can be replaced by a shared sale group')
        return self


class SharedStockSaleApplyRequest(LinkInput):
    preview_hash: StrictStr = Field(pattern=r'^[0-9a-f]{64}$')
    confirmed: Literal[True]

    @field_validator('confirmed', mode='before')
    @classmethod
    def confirmation(cls, value):
        return SharedStockSalePreviewRequest.confirmation(value)
