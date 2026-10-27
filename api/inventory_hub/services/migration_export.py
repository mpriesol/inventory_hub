"""Read-only, consistent local catalog export without editor presentation queries."""
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
import json
import tempfile
import zipfile

from sqlalchemy import select, text

from inventory_hub.db_models import Product, ProductGroup, ProductIdentifier, Shop, Supplier, SupplierProduct, Warehouse
from inventory_hub.db_models_ext import ProductSupplySource, ProductVariantAttribute, ShopProduct, StockBalance
from inventory_hub.product_editor_models import ProductEditorOverride
from inventory_hub.sku_policy_models import ProductSkuIdentity, SkuBrand, SkuBrandAlias
from inventory_hub.stock_tracking_models import StockTracking


# Deliberate field allowlists: never dump configuration, credentials, invoices,
# orders, customer records, arbitrary ORM relationships or whole supplier blobs.
TABLES = {
    "products": (Product, "id sku name brand group_id item_type stock_unit is_active created_from_source created_at updated_at"),
    "groups": (ProductGroup, "id code name"),
    "identifiers": (ProductIdentifier, "id product_id identifier_type value supplier_id is_primary"),
    "variant_attributes": (ProductVariantAttribute, "product_id attribute_name attribute_value display_order"),
    "shops": (Shop, "id code name platform is_active"),
    "shop_links": (ShopProduct, "id shop_id product_id external_product_id external_variant_id external_code parent_code is_variant is_listed shop_availability shop_stock shop_price last_pull_at"),
    "suppliers": (Supplier, "id code name is_active"),
    "supplier_products": (SupplierProduct, "id supplier_id supplier_sku manufacturer_sku ean name brand supplier_group_code is_active is_discontinued supplier_availability_code_raw supplier_availability_text stock_qty lead_time_days last_seen_at updated_at"),
    "supplier_links": (ProductSupplySource, "id product_id supplier_product_id priority is_primary orderable pack_size is_active"),
    "warehouses": (Warehouse, "id code name is_active is_default"),
    # Raw historical balances are explicitly separate from confirmation evidence.
    "balances": (StockBalance, "product_id warehouse_id qty_on_hand qty_reserved qty_quarantined updated_at"),
    "tracking": (StockTracking, "product_id warehouse_id started_at source_type source_id"),
    "sku_brands": (SkuBrand, "id code name kind"),
    "sku_brand_aliases": (SkuBrandAlias, "alias_key brand_id alias"),
    "sku_identities": (ProductSkuIdentity, "product_id brand_id source_code code_kind supplier_id allocated_sku brand_raw"),
}


def json_default(value):
    if isinstance(value, Decimal):
        return str(value)  # Exact decimal values, never binary float rounding.
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(type(value).__name__)


def json_line(value):
    return (json.dumps(value, ensure_ascii=False, default=json_default, allow_nan=False) + "\n").encode("utf-8")


async def build_export(db, remote_products, remote_meta, shop_code):
    """Return a seekable temporary ZIP; caller owns closing it after download."""
    output = tempfile.TemporaryFile(mode="w+b")
    try:
        # This must be the first statement on the dedicated request session.
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        snapshot_at = await db.scalar(text("SELECT transaction_timestamp()"))
        counts = {}
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for name, (model, fields) in TABLES.items():
                columns = [getattr(model, field) for field in fields.split()]
                statement = select(*columns).order_by(*model.__table__.primary_key.columns)
                result = await db.stream(statement.execution_options(yield_per=1000))
                count = 0
                try:
                    with archive.open(name + ".jsonl", "w", force_zip64=True) as member:
                        async for row in result.mappings():
                            member.write(json_line(dict(row)))
                            count += 1
                finally:
                    await result.close()
                counts[name] = count
            # Only operator-visible identity/content overrides, not save receipts
            # or publication internals. Keeps effective names/EAN corrections.
            result = await db.stream(select(ProductEditorOverride.product_id,
                ProductEditorOverride.data["common"].label("common"),
                ProductEditorOverride.data["variant"].label("variant"),
                ProductEditorOverride.data["shops"].label("shops")).order_by(ProductEditorOverride.product_id)
                .execution_options(yield_per=1000))
            count = 0
            try:
                with archive.open("editor_overrides.jsonl", "w", force_zip64=True) as member:
                    async for row in result.mappings():
                        member.write(json_line(dict(row)))
                        count += 1
            finally:
                await result.close()
            counts["editor_overrides"] = count
            with archive.open("upgates_products.jsonl", "w", force_zip64=True) as member:
                for product in remote_products:
                    member.write(json_line(product))
            counts["upgates_products"] = len(remote_products)
            archive.writestr("manifest.json", json_line({
                "schema_version": 1, "generated_at": datetime.now(timezone.utc),
                "local_snapshot_at": snapshot_at, "remote_shop": shop_code,
                "remote": remote_meta, "counts": counts,
                "notes": ["Local tables share one repeatable-read, read-only transaction.",
                    "Remote catalog has its own observation time; this is not a global freeze.",
                    "Balances can be historical and unconfirmed. Tracking evidence is exported separately.",
                    "Shop and supplier quantities are observations, not physical warehouse balances.",
                    "No product, link, quantity, inventory authority or synchronization setting was changed."]}))
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise


def download_chunks(output):
    try:
        while chunk := output.read(65536):
            yield chunk
    finally:
        output.close()
