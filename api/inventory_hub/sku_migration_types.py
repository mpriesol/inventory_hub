"""Bounded, read-only requests for reviewing existing card code changes."""
from pydantic import Field, StrictBool, StrictInt, model_validator

from inventory_hub.sku_policy_types import PolicyModel, SkuIdentityInput


class SkuMigrationItem(PolicyModel):
    product_id: StrictInt = Field(gt=0)
    sku_identity: SkuIdentityInput


class SkuMigrationPreviewRequest(PolicyModel):
    items: list[SkuMigrationItem] = Field(min_length=1, max_length=20)
    verify_remote: StrictBool = False

    @model_validator(mode="after")
    def unique_products(self):
        if len({item.product_id for item in self.items}) != len(self.items):
            raise ValueError("Each existing Hub card must appear only once")
        return self
