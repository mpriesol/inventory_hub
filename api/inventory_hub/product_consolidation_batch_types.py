"""Explicit operator evidence for one atomic, local identity migration."""
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictBool, StrictStr, field_validator, model_validator

from inventory_hub.product_link_types import LinkInput
from inventory_hub.product_consolidation_types import ProductConsolidationPreviewRequest


Digest = Annotated[StrictStr, Field(pattern=r'^[a-f0-9]{64}$')]
NativeID = Annotated[StrictStr, Field(pattern=r'^[1-9][0-9]{0,15}$')]
Code = Annotated[StrictStr, Field(min_length=1, max_length=100)]


class BatchGroup(ProductConsolidationPreviewRequest):
    # An earlier single-card operation UUID must never be reused by this route.
    # The server derives new child UUIDs from the new batch UUID and exact IDs.
    request_id: None = None


class NativeAxis(LinkInput):
    name: StrictStr = Field(min_length=1, max_length=255)
    value: StrictStr = Field(min_length=1, max_length=500)


class NativeLeaf(LinkInput):
    code: Code
    parent_code: Code
    parent_id: NativeID
    is_variant: StrictBool
    external_id: NativeID
    name: StrictStr = Field(max_length=2000)
    eans: list[Annotated[StrictStr, Field(max_length=1000)]] = Field(max_length=100)
    raw_barcodes: list[Annotated[StrictStr, Field(max_length=1000)]] = Field(max_length=100)
    invalid_barcodes: list[Annotated[StrictStr, Field(max_length=1000)]] = Field(max_length=100)
    attributes: list[NativeAxis] = Field(max_length=100)
    warnings: list[Literal['unverified_barcode', 'variant_axes_unknown']] = Field(default_factory=list, max_length=2)
    shop_url: StrictStr | None = Field(default=None, max_length=2000)
    shop_admin_url: StrictStr | None = Field(default=None, max_length=2000)
    supplier_code: Code | None = None
    reviewed_attributes: list[NativeAxis] | None = Field(default=None, max_length=100)
    reviewed_attributes_sha256: Digest | None = None
    reviewed_attributes_source_sha256: Digest | None = None

    @model_validator(mode='after')
    def standalone(self):
        if not self.is_variant and (self.external_id != self.parent_id or self.code != self.parent_code):
            raise ValueError('Standalone identity must match its parent')
        if any(value != value.strip() or any(ord(c) < 32 for c in value) for value in (self.code, self.parent_code)):
            raise ValueError('Exact native codes are required')
        from inventory_hub.services import product_link_source as source
        from inventory_hub.services import product_links as links
        from inventory_hub.services.product_identity import verified_barcodes
        from inventory_hub.services.identifiers import ProductIdentifierService
        if sorted(set(self.eans)) != sorted(verified_barcodes(self.raw_barcodes)):
            raise ValueError('Normalized barcodes must match the retained native value')
        invalid = sorted({value for entry in self.raw_barcodes
            for value, _kind in ProductIdentifierService.split_compound_ean(entry) if not verified_barcodes([value])}
            | {entry for entry in self.raw_barcodes if not ProductIdentifierService.split_compound_ean(entry)})
        if self.invalid_barcodes != invalid:
            raise ValueError('Invalid barcode evidence must match the retained native value')
        if self.reviewed_attributes is not None:
            values = [axis.model_dump() for axis in self.reviewed_attributes]
            if (not values or self.attributes or links.digest(values) != self.reviewed_attributes_sha256
                    or self.reviewed_attributes_source_sha256 is None):
                raise ValueError('Retained axes require their exact separate evidence hashes')
        elif self.reviewed_attributes_sha256 is not None or self.reviewed_attributes_source_sha256 is not None:
            raise ValueError('Retained axis hashes require their attributes')
        for value in (self.shop_url, self.shop_admin_url):
            if value is not None and source._url(value) != value:
                raise ValueError('Native navigation must be a sanitized public URL')
        return self


class AbsentNative(LinkInput):
    is_variant: StrictBool
    parent_id: NativeID
    external_id: NativeID

    @model_validator(mode='after')
    def standalone(self):
        if not self.is_variant and self.external_id != self.parent_id:
            raise ValueError('Standalone absence must name the same product ID')
        return self


class NativeCatalog(LinkInput):
    shop_code: StrictStr = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,49}$')
    fingerprint: Digest
    captured_at: datetime
    export_sha256: Digest
    complete: Literal[True]
    unfiltered_products_export: Literal[True]
    leaves: list[NativeLeaf] = Field(max_length=4480)
    absent_native: list[AbsentNative] = Field(default_factory=list, max_length=3200)
    absent_codes: list[Code] = Field(default_factory=list, max_length=3200)

    @field_validator('complete', 'unfiltered_products_export', mode='before')
    @classmethod
    def explicit(cls, value):
        if value is not True:
            raise ValueError('Explicit complete export acknowledgement required')
        return value

    @field_validator('captured_at')
    @classmethod
    def timezone(cls, value):
        if value.tzinfo is None:
            raise ValueError('Catalog capture time requires a timezone')
        return value

    @model_validator(mode='after')
    def distinct(self):
        keys = [(leaf.is_variant, leaf.external_id) for leaf in self.leaves]
        codes = [leaf.code.casefold() for leaf in self.leaves]
        absent = [(item.is_variant, item.parent_id, item.external_id) for item in self.absent_native]
        if (len(set(keys)) != len(keys) or len(set(codes)) != len(codes)
                or len(set(absent)) != len(absent) or len(set(self.absent_codes)) != len(self.absent_codes)):
            raise ValueError('Catalog evidence must contain distinct identities and codes')
        if set(keys) & {(item.is_variant, item.external_id) for item in self.absent_native}:
            raise ValueError('A selected native leaf cannot also be absent')
        if set(codes) & {code.casefold() for code in self.absent_codes}:
            raise ValueError('A selected native code cannot also be absent')
        return self


class BatchPreviewRequest(LinkInput):
    request_id: UUID
    approved_scope_sha256: Digest
    groups: list[BatchGroup] = Field(min_length=1, max_length=64)
    native_catalogs: list[NativeCatalog] = Field(min_length=1, max_length=10)
    identity_scope_confirmed: Literal[True]
    preserve_all_raw_history: Literal[True]
    keep_catalog_campaigns_paused: Literal[True]

    @field_validator('identity_scope_confirmed', 'preserve_all_raw_history', 'keep_catalog_campaigns_paused', mode='before')
    @classmethod
    def explicit(cls, value):
        if value is not True:
            raise ValueError('Explicit operator confirmation is required')
        return value

    @model_validator(mode='after')
    def disjoint(self):
        ids, mappings, targets, codes = [], [], [], []
        for group in self.groups:
            ids.extend([group.keeper_product_id, *group.retired_product_ids])
            mappings.extend(item.mapping_id for item in group.mapping_actions)
            for target in [a.target for a in group.mapping_actions if a.target] + group.additional_sales:
                targets.append((target.shop_code, target.is_variant,
                    target.native_variant_id if target.is_variant else target.native_product_id))
                codes.append((target.shop_code, target.code.casefold()))
        shops = [catalog.shop_code for catalog in self.native_catalogs]
        if any(len(set(values)) != len(values) for values in (ids, mappings, targets, codes, shops)):
            raise ValueError('Batch groups, mappings, native identities and shops must be disjoint')
        return self


class BatchApplyRequest(LinkInput):
    request: BatchPreviewRequest
    preview_hash: Digest
    confirmed: Literal[True]

    @field_validator('confirmed', mode='before')
    @classmethod
    def explicit(cls, value):
        if value is not True:
            raise ValueError('Explicit confirmation is required')
        return value
