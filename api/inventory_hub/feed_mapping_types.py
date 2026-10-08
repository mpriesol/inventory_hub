"""Bounded, declarative feed mappings. No executable expressions are accepted."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MappingTransform(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["trim", "strip_html", "split", "join", "replace", "decimal", "multiply", "round", "truncate", "map", "prefix", "suffix"]
    value: str | None = Field(default=None, max_length=1000)
    with_: str | None = Field(default=None, alias="with", max_length=1000)
    values: dict[str, str] = Field(default_factory=dict, max_length=1000)

    @field_validator("values")
    @classmethod
    def bounded_map(cls, value):
        if any(len(key) > 1000 or len(item) > 1000 for key, item in value.items()):
            raise ValueError("Value mapping entries are too long")
        return value


class FieldBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=1, max_length=150)
    source: str | None = Field(default=None, max_length=500)
    constant: Any = None
    default: Any = None
    transforms: list[MappingTransform] = Field(default_factory=list, max_length=12)
    param_name_path: str | None = Field(default=None, max_length=200)
    param_value_path: str | None = Field(default=None, max_length=200)
    param_match_name: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def one_source(self):
        if self.source and self.constant is not None:
            raise ValueError("Choose a source field or a constant, not both")
        return self


class CategoryBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=1000)
    target_code: str = Field(min_length=1, max_length=100)


class MappingDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["auto", "xml", "csv", "json"] = "auto"
    record_path: str = Field(default="", max_length=500)
    csv_delimiter: str = Field(default="", max_length=1)
    csv_encoding: Literal["utf-8-sig", "cp1250", "iso-8859-2"] = "utf-8-sig"
    bindings: list[FieldBinding] = Field(default_factory=list, max_length=300)
    category_rules: list[CategoryBinding] = Field(default_factory=list, max_length=5000)
    seo_fallback: bool = True

    @model_validator(mode="after")
    def unique_targets(self):
        keys = [binding.target for binding in self.bindings]
        if len(keys) != len(set(keys)):
            raise ValueError("Each destination can be mapped once")
        categories = [rule.source for rule in self.category_rules]
        if len(categories) != len(set(categories)):
            raise ValueError("Each supplier category can be mapped once per shop")
        if self.csv_delimiter in ("\r", "\n", '"', "\x00"):
            raise ValueError("Invalid CSV delimiter")
        return self


class MappingScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feed_key: str = Field(default="products", pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    shop: str = Field(default="", pattern=r"^[a-zA-Z0-9_-]{0,50}$")


class MappingSave(MappingScope):
    expected_revision: int = Field(ge=0)
    definition: MappingDefinition


class MappingPreview(MappingScope):
    definition: MappingDefinition
    sample_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    limit: int = Field(default=5, ge=1, le=20)


class MappingRemap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feed_key: str = Field(default="products", pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    expected_revision: int = Field(ge=1)
    sample_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
