"""Explicit physical receipt and documented FIFO cost corrections."""
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator


class FifoConfirmedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    confirmed: Literal[True]
    reason: StrictStr = Field(min_length=1, max_length=1000)

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation is required")
        return value

    @field_validator("reason")
    @classmethod
    def meaningful_text(cls, value):
        if value != value.strip() or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("A nonempty reference or reason is required")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError("Invalid reference or reason") from None
        return value


class FifoPieceInput(FifoConfirmedInput):
    quantity: Decimal = Field(gt=0, le=Decimal("999999999"), max_digits=12, decimal_places=3)

    @field_validator("quantity", mode="before")
    @classmethod
    def decimal_piece_text(cls, value):
        if not isinstance(value, str):
            raise ValueError("Quantity must be a decimal string")
        return value

    @field_validator("quantity")
    @classmethod
    def whole_pieces(cls, value):
        if value != value.to_integral_value():
            raise ValueError("Physical order returns support whole pieces")
        return value


class FifoReturnInput(FifoPieceInput):
    issue_movement_id: int = Field(gt=0, strict=True)
    case_reference: StrictStr = Field(min_length=1, max_length=200)
    condition: Literal["good", "damaged"]
    physical_received: Literal[True]

    @field_validator("physical_received", mode="before")
    @classmethod
    def explicit_receipt(cls, value):
        return cls.explicit_confirmation(value)

    @field_validator("case_reference")
    @classmethod
    def reference_text(cls, value):
        return cls.meaningful_text(value)


class FifoReleaseInput(FifoPieceInput):
    source_layer_id: int = Field(gt=0, strict=True)
    target_warehouse_id: int = Field(gt=0, strict=True)
    condition_verified: Literal[True]

    @field_validator("condition_verified", mode="before")
    @classmethod
    def explicit_condition(cls, value):
        return cls.explicit_confirmation(value)


class FifoCostRevisionInput(FifoConfirmedInput):
    root_layer_id: int = Field(gt=0, strict=True)
    expected_revision: int = Field(ge=0, strict=True)
    new_unit_cost: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=4)
    cost_status: Literal["known", "provisional", "unknown"]
    document_reference: StrictStr = Field(min_length=1, max_length=200)

    @field_validator("new_unit_cost", mode="before")
    @classmethod
    def decimal_cost_text(cls, value):
        if value is not None and not isinstance(value, str):
            raise ValueError("Cost must be a decimal string or null")
        return value

    @field_validator("document_reference")
    @classmethod
    def reference_text(cls, value):
        return cls.meaningful_text(value)

    @model_validator(mode="after")
    def explicit_cost_knowledge(self):
        if (self.new_unit_cost is None) != (self.cost_status == "unknown"):
            raise ValueError("Unknown has no cost; known and provisional require a cost")
        return self
