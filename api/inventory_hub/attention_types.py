"""Bounded, credential-free producer and operator contracts."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field

Code = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_.:-]{1,100}$")]
Count = Annotated[int, Field(ge=0, le=10**12)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Coverage(StrictModel):
    target: Code
    rows: Count
    previous_rows: Count
    required_rows: Count
    min_coverage_percent: int = Field(ge=1, le=100)


class SupplierDetails(StrictModel):
    run_id: int = Field(gt=0)
    total: Count = 0
    accepted: Count = 0
    rejected: Count = 0
    unknown: Count = 0
    error_code: Code | None = None
    warnings: list[Code] = Field(default_factory=list, max_length=30)
    coverage: list[Coverage] = Field(default_factory=list, max_length=3)


class AttentionEvent(StrictModel):
    kind: Code
    scope: Code
    sequence: int = Field(gt=0, le=2**63-1)
    active: bool
    details: dict = Field(default_factory=dict)


class AttentionAction(StrictModel):
    expected_revision: int = Field(ge=1)
    action: Literal["ignore", "restore"]


class AttentionRuleInput(StrictModel):
    expected_revision: int = Field(ge=0)
    enabled: bool
    min_occurrences: int = Field(ge=1, le=100)
