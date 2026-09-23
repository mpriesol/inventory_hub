"""Bounded operational controls; stock authority is never inherited."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator


class OperationalValues(BaseModel):
    model_config = ConfigDict(extra="forbid")
    poll_interval_seconds: StrictInt = Field(default=300, ge=60, le=86400)
    reconcile_interval_hours: StrictInt = Field(default=24, ge=1, le=168)
    overlap_minutes: StrictInt = Field(default=10, ge=5, le=1440)
    reconcile_window_days: StrictInt = Field(default=7, ge=1, le=30)
    max_pages_per_pass: StrictInt = Field(default=100, ge=1, le=100)
    run_timeout_seconds: StrictInt = Field(default=180, ge=30, le=180)
    retry_base_seconds: StrictInt = Field(default=300, ge=60, le=3600)
    retry_max_seconds: StrictInt = Field(default=3600, ge=300, le=86400)
    processing_batch_size: StrictInt = Field(default=20, ge=1, le=100)
    processing_retry_minutes: StrictInt = Field(default=5, ge=1, le=1440)
    full_order_check_hours: StrictInt = Field(default=24, ge=1, le=168)

    @model_validator(mode="after")
    def retry_range(self):
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("Retry maximum must be at least the base")
        return self


class ConfirmedSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: StrictInt = Field(ge=0)
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit confirmation required")
        return value


class WarehouseSettingsInput(ConfirmedSettings):
    warehouse_code: StrictStr = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}$")
    values: OperationalValues
    processing_paused: StrictBool


class ShopSettingsInput(ConfirmedSettings):
    shop_code: StrictStr = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,49}$")
    expected_warehouse_revision: StrictInt = Field(ge=0)
    overrides: dict[StrictStr, StrictInt]
    mode: Literal["manual", "reserve", "fulfill"]
    fulfillment_confirmed: StrictBool = False

    @field_validator("overrides")
    @classmethod
    def validate_overrides(cls, values):
        # Validate fields/ranges independently; cross-field validation uses resolved warehouse values.
        for key, value in values.items():
            if key not in OperationalValues.model_fields:
                raise ValueError("Unknown setting")
            check = {key: value}
            if key == "retry_base_seconds":
                check["retry_max_seconds"] = 86400
            OperationalValues(**check)
        return values
