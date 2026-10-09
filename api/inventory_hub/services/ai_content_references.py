"""Read a shop product as a reviewable example, without any paid call or writes."""
import asyncio
from datetime import datetime, timezone

from pydantic import Field, ValidationError

from inventory_hub.ai_content_types import ReferenceProduct, StrictModel
from inventory_hub.services.ai_content_existing import localized, source_snapshot
from inventory_hub.services.ai_content_update import read_product
from inventory_hub.services.ai_content_validation import text_of
from inventory_hub.services.catalog import CatalogError
from inventory_hub.services.catalog_html import clean_description
from inventory_hub.services.catalog_import import shop_config
from inventory_hub.services.upgates import UpgatesClient


class ReferenceRequest(StrictModel):
    shop: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    code: str = Field(min_length=1, max_length=100)


def snapshot(remote, shop):
    base = source_snapshot(remote, "sk")
    description = localized(remote.get("descriptions"), "sk")
    content = {key: base["descriptions"][key] for key in
               ("title", "short_description", "seo_title")}
    content.update(long_description=clean_description(description.get("long_description", "")),
                   meta_description=base["descriptions"]["seo_description"], parameters=base["parameters"])
    for meta in remote.get("metas") or []:
        if meta.get("key") in ("h1_descriptor", "future_name", "h1_descr_suffix"):
            values = meta.get("values") or []
            value = values.get("sk", "") if isinstance(values, dict) else localized(values, "sk").get("value", "")
            content[meta["key"]] = text_of(str(meta.get("value", value) or ""))
    try:
        return ReferenceProduct(shop=shop, code=base["code"], product_id=base["product_id"],
            captured_at=datetime.now(timezone.utc).isoformat(), content=content).model_dump(mode="json")
    except ValidationError:
        raise CatalogError("ai_reference_too_large", "The reference exceeds the supported content size; select a smaller example", 422) from None


async def load(request):
    shop_config(request.shop)
    remote = await asyncio.to_thread(read_product, UpgatesClient.from_shop(request.shop),
                                   request.code, include_parameters=True)
    return snapshot(remote, request.shop)
