"""Explicit dictionary changes and generic card availability override."""
from datetime import datetime
from uuid import UUID
from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator, model_validator
from inventory_hub.product_editor_types import EditorModel, safe_text


class CatalogChange(EditorModel):
    request_id: UUID


class AvailabilityChange(CatalogChange):
    id: StrictInt | None = Field(default=None, gt=0)
    expected_revision: StrictInt = Field(ge=0)
    label: StrictStr
    is_active: StrictBool = True
    sort_order: StrictInt = Field(default=0, ge=-100000, le=100000)

    @field_validator('label')
    @classmethod
    def valid_label(cls, value):
        value = safe_text(value, maximum=100, blank=False).strip()
        if any(ord(char) < 32 for char in value):
            raise ValueError('Invalid label')
        return value


class WarehouseChange(CatalogChange):
    code: StrictStr = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,49}$')
    expected_updated_at: datetime | None = None
    name: StrictStr
    address: StrictStr | None = None
    is_active: StrictBool = True
    is_default: StrictBool = False

    @field_validator('name')
    @classmethod
    def valid_name(cls, value):
        return safe_text(value, maximum=255, blank=False).strip()

    @field_validator('address')
    @classmethod
    def valid_address(cls, value):
        return safe_text(value, maximum=2000)


class AvailabilityOverrideChange(EditorModel):
    product_id: StrictInt = Field(gt=0)
    expected_revision: StrictInt = Field(ge=0)
    label: StrictStr | None = Field(default=None, max_length=100)
    visible: StrictBool | None = None
    clear: StrictBool = False

    @model_validator(mode='after')
    def changes(self):
        if self.clear and self.model_fields_set & {'label', 'visible'}:
            raise ValueError('Clear cannot include values')
        if not self.clear and not self.model_fields_set & {'label', 'visible'}:
            raise ValueError('No changes')
        return self


class AvailabilityOverrideInput(CatalogChange):
    changes: list[AvailabilityOverrideChange] = Field(min_length=1, max_length=100)
