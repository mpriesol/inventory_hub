"""One explicit physical identity, complete mapping actions, no stock operation."""
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID
from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator
from inventory_hub.product_link_types import LinkInput
from inventory_hub.shared_stock_sale_types import SharedStockSaleApplyRequest, SharedStockSaleTarget


class ConsolidationTarget(SharedStockSaleTarget):
    shop_code: StrictStr = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,49}$')
    sale_to_stock_factor: Literal[1] = 1
    replace_deleted_mapping: None = None

    @field_validator('sale_to_stock_factor', mode='before')
    @classmethod
    def whole_unit(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError('Consolidation only preserves one unchanged complete retail unit')
        return value


class ConsolidationMappingAction(LinkInput):
    mapping_id: StrictInt = Field(gt=0)
    action: Literal['retain', 'bind', 'retire_absent', 'retire_unverified_code_absent']
    target: ConsolidationTarget | None = None

    @model_validator(mode='after')
    def shape(self):
        if (self.action in ('retire_absent', 'retire_unverified_code_absent')) != (self.target is None):
            raise ValueError('Retire an absent mapping or name its exact current native leaf')
        return self


class ConsolidationEvidence(LinkInput):
    review_note: StrictStr = Field(min_length=20, max_length=3000)
    source_urls: list[StrictStr] = Field(min_length=1, max_length=10)
    physical_unit: StrictStr = Field(min_length=5, max_length=500)

    @field_validator('review_note', 'physical_unit')
    @classmethod
    def clean_text(cls, value):
        if value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError('Supply plain reviewed physical identity evidence')
        return value

    @field_validator('source_urls')
    @classmethod
    def public_sources(cls, values):
        for value in values:
            parsed = urlsplit(value)
            if (len(value) > 2000 or parsed.scheme != 'https' or not parsed.hostname
                    or parsed.username or parsed.password or any(ord(c) < 33 for c in value)):
                raise ValueError('Supply public HTTPS identity evidence without credentials')
        return sorted(set(values))


class ProductConsolidationPreviewRequest(LinkInput):
    request_id: UUID
    keeper_product_id: StrictInt = Field(gt=0)
    retired_product_ids: list[StrictInt] = Field(min_length=1, max_length=9)
    mode: Literal['identity_only_preserve_unconfirmed_history']
    same_physical_retail_unit_confirmed: Literal[True]
    legacy_initial_history_preserved: Literal[True]
    preserve_keeper_invoice_receiving: StrictBool = False
    use_keeper_supplier_gtin: StrictBool = False
    identity_evidence: ConsolidationEvidence
    mapping_actions: list[ConsolidationMappingAction] = Field(max_length=50)
    additional_sales: list[ConsolidationTarget] = Field(default_factory=list, max_length=20)

    @field_validator('same_physical_retail_unit_confirmed', 'legacy_initial_history_preserved', mode='before')
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError('Explicit review confirmation is required')
        return value

    @model_validator(mode='after')
    def distinct(self):
        if (any(pid <= 0 for pid in self.retired_product_ids)
                or self.keeper_product_id in self.retired_product_ids
                or len(set(self.retired_product_ids)) != len(self.retired_product_ids)):
            raise ValueError('Select distinct existing physical cards')
        mapping_ids = [row.mapping_id for row in self.mapping_actions]
        if len(set(mapping_ids)) != len(mapping_ids):
            raise ValueError('Review each original mapping exactly once')
        targets = [row.target for row in self.mapping_actions if row.target] + self.additional_sales
        keys = [(row.shop_code, row.is_variant, row.native_variant_id if row.is_variant else row.native_product_id)
                for row in targets]
        codes = [(row.shop_code, row.code.casefold()) for row in targets]
        if len(set(keys)) != len(keys) or len(set(codes)) != len(codes):
            raise ValueError('Select each native identity/code once per shop')
        return self


class ProductConsolidationApplyRequest(SharedStockSaleApplyRequest):
    pass
