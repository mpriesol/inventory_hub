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


class ProductLinkMatch(LinkInput):
    product_id: StrictInt = Field(gt=0)
    code: StrictStr
    parent_code: StrictStr
    is_variant: StrictBool

    @field_validator('code', 'parent_code')
    @classmethod
    def code_value(cls, value):
        return exact_code(value)

    @model_validator(mode='after')
    def standalone_parent(self):
        if not self.is_variant and self.parent_code != self.code:
            raise ValueError('A standalone product is its own parent')
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
