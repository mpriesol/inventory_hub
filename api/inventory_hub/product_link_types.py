"""Explicit, bounded links between existing Hub cards and existing shop leaves."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator


def exact_code(value: str) -> str:
    if (not value or len(value) > 100 or value != value.strip() or ';' in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('Use one exact product code, without separators or control characters')
    value.encode('utf-8')
    return value


class LinkInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ReviewedIdentitySource(LinkInput):
    kind: Literal['shop_mapping', 'supplier_product']
    id: StrictInt = Field(gt=0)


class ReviewedNativeTarget(LinkInput):
    mapping_id: StrictInt = Field(gt=0)
    native_product_id: StrictInt = Field(gt=0, le=9007199254740991)
    native_variant_id: StrictInt | None = Field(default=None, gt=0, le=9007199254740991)
    identity_evidence: StrictStr = Field(min_length=10, max_length=2000)
    same_physical_item_confirmed: Literal[True]

    @field_validator('same_physical_item_confirmed', mode='before')
    @classmethod
    def confirmation(cls, value):
        if value is not True:
            raise ValueError('Explicit confirmation is required')
        return value

    @field_validator('identity_evidence')
    @classmethod
    def evidence(cls, value):
        if value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('Describe the verified physical identity and its source')
        value.encode('utf-8')
        return value


class ReviewedDeletedReplacement(ReviewedNativeTarget):
    mode: Literal['verified_barcode_subset', 'exact_sku_without_barcode']
    evidence_source: ReviewedIdentitySource | None = None

    @model_validator(mode='after')
    def source_required(self):
        if self.mode == 'exact_sku_without_barcode' and self.evidence_source is None:
            raise ValueError('Pin an existing other-shop mapping or bound supplier item')
        return self


class CompleteUnverifiedParent(ReviewedNativeTarget):
    native_variant_id: StrictInt = Field(gt=0, le=9007199254740991)
    expected_parent_code: StrictStr

    @field_validator('expected_parent_code')
    @classmethod
    def parent_code_value(cls, value):
        return exact_code(value)


class ReviewedParentRename(CompleteUnverifiedParent):
    """A known native variant retained under a renamed native parent code."""


class ProductLinkMatch(LinkInput):
    product_id: StrictInt = Field(gt=0)
    code: StrictStr
    parent_code: StrictStr
    is_variant: StrictBool
    replace_deleted_variant: StrictBool = False
    replace_deleted_product: StrictBool = False
    reviewed_deleted_replacement: ReviewedDeletedReplacement | None = None
    complete_unverified_parent: CompleteUnverifiedParent | None = None
    reviewed_parent_rename: ReviewedParentRename | None = None

    @field_validator('code', 'parent_code')
    @classmethod
    def code_value(cls, value):
        return exact_code(value)

    @model_validator(mode='after')
    def standalone_parent(self):
        if not self.is_variant and self.parent_code != self.code:
            raise ValueError('A standalone product is its own parent')
        if self.replace_deleted_variant and self.replace_deleted_product:
            raise ValueError('Choose the kind of the original deleted mapping')
        modes = [self.replace_deleted_variant, self.replace_deleted_product,
                 self.reviewed_deleted_replacement is not None, self.complete_unverified_parent is not None,
                 self.reviewed_parent_rename is not None]
        if sum(modes) > 1:
            raise ValueError('Choose one explicit recovery operation')
        review = self.reviewed_deleted_replacement or self.complete_unverified_parent or self.reviewed_parent_rename
        if review is not None and self.is_variant != (review.native_variant_id is not None):
            raise ValueError('Pin the exact native identity for the selected leaf kind')
        if self.complete_unverified_parent is not None and (
                not self.is_variant or self.parent_code == self.complete_unverified_parent.expected_parent_code):
            raise ValueError('Parent completion requires a different verified variant container code')
        if self.reviewed_parent_rename is not None and (
                not self.is_variant or self.parent_code == self.reviewed_parent_rename.expected_parent_code):
            raise ValueError('Parent rename requires an existing variant and a different exact parent code')
        return self


class ProductLinksCandidatesRequest(LinkInput):
    shop_code: StrictStr = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,49}$')
    product_ids: list[StrictInt] = Field(min_length=1, max_length=100)
    query: StrictStr = Field(default='', max_length=100)

    @field_validator('product_ids')
    @classmethod
    def ids(cls, value):
        if any(item <= 0 for item in value) or len(set(value)) != len(value):
            raise ValueError('Choose distinct existing product IDs')
        return value


class ProductLinksPreviewRequest(ProductLinksCandidatesRequest):
    request_id: UUID
    manual_matches: list[ProductLinkMatch] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def selected_matches(self):
        ids = [item.product_id for item in self.manual_matches]
        if len(set(ids)) != len(ids) or not set(ids) <= set(self.product_ids) or self.query:
            raise ValueError('Manual choices must identify distinct selected rows')
        if any(item.reviewed_parent_rename is not None for item in self.manual_matches) and (
                set(ids) != set(self.product_ids)
                or any(item.reviewed_parent_rename is None for item in self.manual_matches)):
            raise ValueError('Parent rename is an explicit metadata-only batch without mixed link operations')
        return self


class ProductLinksRefreshRequest(LinkInput):
    shop_code: StrictStr = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,49}$')
    product_ids: list[StrictInt] = Field(min_length=1, max_length=100)

    @field_validator('product_ids')
    @classmethod
    def ids(cls, value):
        return ProductLinksCandidatesRequest.ids(value)


class ProductLinksApplyRequest(LinkInput):
    preview_hash: StrictStr = Field(pattern=r'^[0-9a-f]{64}$')
    product_ids: list[StrictInt] = Field(min_length=1, max_length=100)
    confirmed: Literal[True]

    @field_validator('product_ids')
    @classmethod
    def ids(cls, value):
        return ProductLinksCandidatesRequest.ids(value)

    @field_validator('confirmed', mode='before')
    @classmethod
    def confirmation(cls, value):
        if value is not True:
            raise ValueError('Explicit confirmation is required')
        return value
