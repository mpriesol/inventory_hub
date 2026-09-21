"""Versioned content rules and contracts. AI never owns prices, stock or identity."""
from __future__ import annotations

import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from inventory_hub.catalog_types import ShopImportOptions


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Policy(StrictModel):
    review_required: bool | None = None
    active_after_import: bool | None = None
    show_cost_estimate: bool | None = None
    confirm_import: bool | None = None


class Scope(StrictModel):
    shop: str = ""
    supplier: str = ""
    category: str = ""
    brand: str = ""
    product: str = ""


class Rule(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=200)
    scope: Scope = Field(default_factory=Scope)
    instructions: str = Field(default="", max_length=24000)
    policy: Policy = Field(default_factory=Policy)
    enabled: bool = True
    official_domains: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def domains(self):
        for domain in self.official_domains:
            if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}", domain):
                raise ValueError("Use public domain names without URLs or credentials")
        return self


class ParameterDefinition(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    required: bool = False
    scope: Literal["parent", "variant"] = "parent"
    values: list[str] = Field(default_factory=list, max_length=1000)
    unit: str = Field(default="", max_length=40)
    instructions: str = Field(default="", max_length=2000)


class CategoryProfile(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=200)
    instructions: str = Field(default="", max_length=24000)
    parameters: list[ParameterDefinition] = Field(default_factory=list, max_length=100)
    shop_categories: dict[str, str] = Field(default_factory=dict)
    policy: Policy = Field(default_factory=Policy)
    automatic_import_ready: bool = True

    @model_validator(mode="after")
    def unique_parameters(self):
        names = [p.name.casefold() for p in self.parameters]
        if len(set(names)) != len(names):
            raise ValueError("Parameter names must be unique within a category")
        return self


class RuleBook(StrictModel):
    rules: list[Rule] = Field(default_factory=list, max_length=300)
    categories: list[CategoryProfile] = Field(default_factory=list, max_length=300)

    @model_validator(mode="after")
    def unique_ids(self):
        for values in (self.rules, self.categories):
            if len({v.id for v in values}) != len(values):
                raise ValueError("Duplicate rule/category ID")
        scopes = [tuple(r.scope.model_dump().values()) for r in self.rules if r.enabled]
        if len(set(scopes)) != len(scopes):
            raise ValueError("Combine rules with identical scopes into one rule")
        return self


class RuleSave(StrictModel):
    expected_published: int
    book: RuleBook
    note: str = Field(min_length=1, max_length=500)


class PublishRequest(StrictModel):
    expected_published: int


class Target(StrictModel):
    shop: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    options: ShopImportOptions = Field(default_factory=ShopImportOptions)
    policy: Policy = Field(default_factory=Policy)


class BatchRequest(StrictModel):
    request_id: UUID
    supplier: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    feed_key: str = Field(default="products", pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    run_id: int | None = None
    product_ids: list[int] = Field(min_length=1, max_length=500)
    ai_product_ids: list[int] = Field(default_factory=list, max_length=500)
    category_profiles: dict[int, str] = Field(default_factory=dict)
    targets: list[Target] = Field(min_length=1, max_length=5)
    research: Literal["official", "feed_only"] = "official"
    sale_price_overrides: dict[int, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def selection(self):
        if len(set(self.product_ids)) != len(self.product_ids) or any(p <= 0 for p in self.product_ids):
            raise ValueError("Select distinct positive product IDs")
        if set(self.ai_product_ids) - set(self.product_ids) or set(self.category_profiles) - set(self.product_ids):
            raise ValueError("AI and category assignments must belong to the selected products")
        if len({t.shop for t in self.targets}) != len(self.targets):
            raise ValueError("Select each shop once")
        return self


class ParameterValue(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    values: list[str] = Field(min_length=1, max_length=1000)
    product_id: int | None


class Evidence(StrictModel):
    claim: str = Field(min_length=1, max_length=2000)
    source: str = Field(min_length=1, max_length=2000)
    quote: str = Field(min_length=1, max_length=3000)


class Content(StrictModel):
    title: str = Field(min_length=1, max_length=250)
    short_description: str = Field(min_length=1, max_length=1500)
    long_description: str = Field(min_length=1, max_length=50000)
    seo_title: str = Field(min_length=1, max_length=250)
    meta_description: str = Field(min_length=1, max_length=1000)
    h1_descriptor: str = Field(max_length=250)
    future_name: str = Field(max_length=250)
    h1_descr_suffix: str = Field(max_length=250)
    parameters: list[ParameterValue] = Field(max_length=500)
    evidence: list[Evidence] = Field(min_length=1, max_length=150)
    warnings: list[str] = Field(max_length=100)
    missing_facts: list[str] = Field(max_length=100)


class ContentReview(StrictModel):
    expected_revision: int
    content: Content
    approve: bool = False


class JobAction(StrictModel):
    expected_revision: int
    action: Literal["start", "import", "cancel", "retry_import"]


class RuleProposal(StrictModel):
    request_id: UUID
    rule_id: str
    category: bool = False
    request: str = Field(min_length=1, max_length=6000)


class ProposedInstructions(StrictModel):
    instructions: str
    reason: str
    questions: list[str]
    parameters: list[ParameterDefinition] | None


class SelectionRequest(StrictModel):
    supplier: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    feed_key: str = Field(default="products", pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    run_id: int | None = None
    product_ids: list[int] = Field(min_length=1, max_length=500)


class PriceReview(StrictModel):
    expected_revision: int
    sale_price_overrides: dict[int, str]
