"""Bounded export-only configuration and one-preview overrides."""

from uuid import UUID
from typing import Literal
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from inventory_hub.services.atomer_availability_source import safe_text


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Configure(Input):
    request_id: UUID
    expected_revision: StrictInt = Field(ge=0)
    enabled: StrictBool = True
    warehouse_code: StrictStr = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}$")
    feed_url: StrictStr | None = Field(default=None, max_length=4000)
    admin_export_url: StrictStr | None = Field(default=None, max_length=4000)
    admin_complete: StrictBool = False

    @field_validator("feed_url", "admin_export_url")
    @classmethod
    def endpoint(cls, value):
        from urllib.parse import urlsplit

        if value in (None, ""):
            return value
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
            ):
                raise ValueError()
        except ValueError:
            raise ValueError(
                "Use an HTTPS export URL without embedded login or fragment"
            ) from None
        return value


class RunInput(Input):
    request_id: UUID
    warehouse_code: StrictStr | None = Field(
        default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}$"
    )
    product_ids: list[StrictInt] | None = Field(
        default=None, min_length=1, max_length=5000
    )
    receiving_batch_id: UUID | None = None

    @field_validator("product_ids")
    @classmethod
    def identifiers(cls, value):
        if value is not None and (
            any(v <= 0 for v in value) or len(value) != len(set(value))
        ):
            raise ValueError("Use unique positive product IDs")
        return value


class Override(Input):
    code: StrictStr = Field(min_length=1, max_length=255)
    visibility: StrictBool | None = None
    include_in_export: StrictBool | None = None
    apply_to_every_export: StrictBool | None = None
    availability: StrictStr | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("code", "availability")
    @classmethod
    def literal(cls, value):
        if value is not None and (not safe_text(value, 255) or value != value.strip()):
            raise ValueError("Use a literal value without control characters")
        return value


class ExportInput(Input):
    expected_revision: StrictInt = Field(ge=1)
    overrides: list[Override] = Field(default_factory=list, max_length=5000)

    @field_validator("overrides")
    @classmethod
    def unique(cls, value):
        if len({v.code for v in value}) != len(value):
            raise ValueError("Duplicate override code")
        return value


class RowFilter(Input):
    view: Literal["all", "changes", "issues"] = "all"
    search: StrictStr = Field(default="", max_length=255)
    kind: Literal["all", "parent", "variant", "standalone"] = "all"
    status: Literal["all", "changed", "unchanged", "context", "blocked", "unmatched"] = "all"
    visibility: Literal["all", "visible", "hidden", "unknown"] = "all"
    current_visibility: Literal["all", "visible", "hidden", "unknown"] = "all"
    proposed_visibility: Literal["all", "visible", "hidden", "unknown"] = "all"
    availability: StrictStr | None = Field(default=None, max_length=100)
    current_availability: StrictStr | None = Field(default=None, max_length=100)
    proposed_availability: StrictStr | None = Field(default=None, max_length=100)
    include_in_export: StrictBool | None = None
    manual_override: StrictBool | None = None


class Selection(Input):
    codes: list[StrictStr] = Field(default_factory=list, max_length=5000)
    all_filtered: StrictBool = False
    filter: RowFilter = Field(default_factory=RowFilter)
    excluded_codes: list[StrictStr] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def scope(self):
        if self.all_filtered == bool(self.codes):
            raise ValueError("Choose explicit codes or the complete filtered result")
        for values in (self.codes, self.excluded_codes):
            if len(values) != len(set(values)) or any(not safe_text(v) or not v for v in values):
                raise ValueError("Use unique exact catalog codes")
        return self


class Changes(Input):
    visibility: StrictBool | None = None
    availability: StrictStr | None = Field(default=None, min_length=1, max_length=100)
    include_in_export: StrictBool | None = None
    apply_to_every_export: StrictBool | None = None

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_dump(exclude_none=True):
            raise ValueError("Choose at least one change")
        if self.availability is not None and (
            not safe_text(self.availability, 100) or self.availability != self.availability.strip()
        ):
            raise ValueError("Use a registered literal availability")
        return self


class EditInput(Input):
    request_id: UUID
    expected_revision: StrictInt = Field(ge=1)
    selection: Selection
    changes: Changes
