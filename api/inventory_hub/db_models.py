# inventory_hub/db_models.py
"""
SQLAlchemy ORM Models for Inventory Hub v12 FINAL.

30 tables matching schema_v12_FINAL.sql exactly.
"""
from __future__ import annotations
from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List, TYPE_CHECKING
import enum

from sqlalchemy import (
    String, Integer, BigInteger, Boolean, Text, Date, DateTime,
    Numeric, ForeignKey, Index, CheckConstraint, UniqueConstraint,
    Enum as SQLEnum, JSON, event, func
)
from sqlalchemy.orm import (
    Mapped, mapped_column, relationship, validates
)
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.ext.hybrid import hybrid_property

from inventory_hub.database import Base

# ============================================================================
# ENUMS (matching PostgreSQL ENUMs)
# ============================================================================

class AvailabilityStatus(str, enum.Enum):
    in_stock = "in_stock"
    low_stock = "low_stock"
    available_3_5_days = "available_3_5_days"
    available_1_2_weeks = "available_1_2_weeks"
    on_order = "on_order"
    unavailable = "unavailable"
    discontinued = "discontinued"
    unknown = "unknown"


class MovementType(str, enum.Enum):
    RECEIVING_IN = "RECEIVING_IN"
    SALE_OUT = "SALE_OUT"
    RETURN_IN = "RETURN_IN"
    ADJUSTMENT_IN = "ADJUSTMENT_IN"
    ADJUSTMENT_OUT = "ADJUSTMENT_OUT"
    TRANSFER_IN = "TRANSFER_IN"
    TRANSFER_OUT = "TRANSFER_OUT"
    WRITE_OFF = "WRITE_OFF"
    INITIAL = "INITIAL"


class ReceivingStatus(str, enum.Enum):
    new = "new"
    in_progress = "in_progress"
    paused = "paused"
    completed = "completed"
    cancelled = "cancelled"


class InventoryCountStatus(str, enum.Enum):
    draft = "draft"
    in_progress = "in_progress"
    review = "review"
    finalized = "finalized"
    cancelled = "cancelled"


class InventoryCountType(str, enum.Enum):
    full = "full"
    partial = "partial"
    cycle = "cycle"
    spot = "spot"


class IdentifierType(str, enum.Enum):
    ean = "ean"
    upc = "upc"
    unverified_barcode = "unverified_barcode"
    supplier_sku = "supplier_sku"
    internal_sku = "internal_sku"
    manufacturer = "manufacturer"
    custom = "custom"


class OrderStatus(str, enum.Enum):
    new = "new"
    processing = "processing"
    shipped = "shipped"
    completed = "completed"
    cancelled = "cancelled"
    returned = "returned"


class ReservationStatus(str, enum.Enum):
    reserved = "reserved"
    backorder = "backorder"
    fulfilled = "fulfilled"
    cancelled = "cancelled"
    rejected = "rejected"


class OversellMode(str, enum.Enum):
    allow = "allow"
    block = "block"


class ScanSessionType(str, enum.Enum):
    receiving = "receiving"
    inventory = "inventory"
    lookup = "lookup"
    adjustment = "adjustment"


class ScanStatus(str, enum.Enum):
    active = "active"
    undone = "undone"


class ConfigEntityType(str, enum.Enum):
    supplier = "supplier"
    shop = "shop"
    warehouse = "warehouse"
    system = "system"


class FeedRunStatus(str, enum.Enum):
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class SyncOutboxStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class AvailabilityCode(str, enum.Enum):
    in_stock = "in_stock"
    low_stock = "low_stock"
    supplier_1_3_days = "supplier_1_3_days"
    supplier_3_5_days = "supplier_3_5_days"
    supplier_1_2_weeks = "supplier_1_2_weeks"
    supplier_on_order = "supplier_on_order"
    check_availability = "check_availability"
    unavailable = "unavailable"
    manual_override = "manual_override"


class StockPositionCode(str, enum.Enum):
    local = "local"
    supplier = "supplier"
    none = "none"


class WarehouseAvailabilityPolicy(str, enum.Enum):
    default_only = "default_only"
    selected = "selected"
    sum_all = "sum_all"


class ShopWarehouseRole(str, enum.Enum):
    fulfillment = "fulfillment"
    availability_source = "availability_source"
    both = "both"


# ============================================================================
# MIXIN for updated_at
# ============================================================================

class TimestampMixin:
    """Mixin for created_at and updated_at columns."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        onupdate=func.now(),
        nullable=False
    )


# ============================================================================
# 1. WAREHOUSES
# ============================================================================

class Warehouse(TimestampMixin, Base):
    __tablename__ = "warehouses"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[Optional[str]] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    
    # Relationships
    stock_balances: Mapped[List["StockBalance"]] = relationship(back_populates="warehouse")
    stock_movements: Mapped[List["StockMovement"]] = relationship(back_populates="warehouse")
    receiving_sessions: Mapped[List["ReceivingSession"]] = relationship(back_populates="warehouse")
    
    __table_args__ = (
        Index("idx_warehouses_single_default", "is_default", unique=True, 
              postgresql_where=(is_default == True)),
    )


# ============================================================================
# 2. SUPPLIERS
# ============================================================================

class Supplier(TimestampMixin, Base):
    __tablename__ = "suppliers"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    config_path: Mapped[Optional[str]] = mapped_column(String(500))
    adapter: Mapped[Optional[str]] = mapped_column(String(100))
    invoice_prefix: Mapped[Optional[str]] = mapped_column(String(20))
    default_currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    
    # Relationships
    products: Mapped[List["Product"]] = relationship(back_populates="supplier")
    supplier_products: Mapped[List["SupplierProduct"]] = relationship(back_populates="supplier")
    supplier_feeds: Mapped[List["SupplierFeed"]] = relationship(back_populates="supplier")
    receiving_sessions: Mapped[List["ReceivingSession"]] = relationship(back_populates="supplier")


# ============================================================================
# 3. SHOPS
# ============================================================================

class Shop(TimestampMixin, Base):
    __tablename__ = "shops"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    config_path: Mapped[Optional[str]] = mapped_column(String(500))
    warehouse_availability_policy: Mapped[WarehouseAvailabilityPolicy] = mapped_column(
        SQLEnum(WarehouseAvailabilityPolicy, name="warehouse_availability_policy"),
        default=WarehouseAvailabilityPolicy.default_only,
        nullable=False
    )
    oversell_mode: Mapped[OversellMode] = mapped_column(
        SQLEnum(OversellMode, name="oversell_mode"),
        default=OversellMode.allow,
        nullable=False
    )
    sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sync_interval_min: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_sync_status: Mapped[Optional[str]] = mapped_column(String(50))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    
    # Relationships
    shop_warehouses: Mapped[List["ShopWarehouse"]] = relationship(back_populates="shop")
    shop_products: Mapped[List["ShopProduct"]] = relationship(back_populates="shop")
    shop_orders: Mapped[List["ShopOrder"]] = relationship(back_populates="shop")
    availability_profiles: Mapped[List["AvailabilityProfile"]] = relationship(back_populates="shop")


# ============================================================================
# 4. SHOP WAREHOUSES
# ============================================================================

class ShopWarehouse(Base):
    __tablename__ = "shop_warehouses"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    role: Mapped[ShopWarehouseRole] = mapped_column(
        SQLEnum(ShopWarehouseRole, name="shop_warehouse_role"),
        default=ShopWarehouseRole.both,
        nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    
    # Relationships
    shop: Mapped["Shop"] = relationship(back_populates="shop_warehouses")
    warehouse: Mapped["Warehouse"] = relationship()
    
    __table_args__ = (
        UniqueConstraint("shop_id", "warehouse_id", name="uq_shop_warehouses"),
        Index("idx_shop_warehouses_shop", "shop_id", postgresql_where="is_active = true"),
    )


# ============================================================================
# 5. SUPPLIER FEED RUNS
# ============================================================================

class SupplierFeedRun(Base):
    __tablename__ = "supplier_feed_runs"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    feed_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("supplier_feeds.id", ondelete="CASCADE"), nullable=False)
    run_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[FeedRunStatus] = mapped_column(
        SQLEnum(FeedRunStatus, name="feed_run_status"),
        default=FeedRunStatus.running,
        nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    source_etag: Mapped[Optional[str]] = mapped_column(String(255))
    source_last_modified: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    source_size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    items_fetched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_new: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_updated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_unchanged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_error: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    error_details: Mapped[Optional[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    
    # Relationships
    feed: Mapped["SupplierFeed"] = relationship(back_populates="runs")
    raw_items: Mapped[List["SupplierFeedItemRaw"]] = relationship(back_populates="run")
    
    __table_args__ = (
        UniqueConstraint("feed_id", "run_number", name="idx_feed_runs_number"),
        Index("idx_feed_runs_feed", "feed_id"),
        Index("idx_feed_runs_status", "feed_id", "status"),
        Index("idx_feed_runs_started", "started_at"),
    )


# ============================================================================
# 6. SUPPLIER FEEDS
# ============================================================================

class SupplierFeed(TimestampMixin, Base):
    __tablename__ = "supplier_feeds"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    feed_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    source_format: Mapped[Optional[str]] = mapped_column(String(50))
    mapping_config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    fetch_interval_min: Mapped[int] = mapped_column(Integer, default=360, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Cache columns (no FK to avoid cyclic dependency)
    last_run_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_run_status: Mapped[Optional[FeedRunStatus]] = mapped_column(SQLEnum(FeedRunStatus, name="feed_run_status"))
    last_run_items_count: Mapped[Optional[int]] = mapped_column(Integer)
    
    # Relationships
    supplier: Mapped["Supplier"] = relationship(back_populates="supplier_feeds")
    runs: Mapped[List["SupplierFeedRun"]] = relationship(back_populates="feed")
    raw_items: Mapped[List["SupplierFeedItemRaw"]] = relationship(back_populates="feed")
    supplier_products: Mapped[List["SupplierProduct"]] = relationship(back_populates="source_feed")
    
    __table_args__ = (
        UniqueConstraint("supplier_id", "code", name="uq_supplier_feeds_code"),
        Index("idx_supplier_feeds_active", "supplier_id", postgresql_where="is_active = true"),
    )


# ============================================================================
# 7. SUPPLIER FEED ITEMS RAW
# ============================================================================

class SupplierFeedItemRaw(Base):
    __tablename__ = "supplier_feed_items_raw"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    feed_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("supplier_feeds.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("supplier_feed_runs.id", ondelete="CASCADE"), nullable=False)
    item_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    processing_error: Mapped[Optional[str]] = mapped_column(Text)
    supplier_product_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("supplier_products.id", ondelete="SET NULL"))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    
    # Relationships
    feed: Mapped["SupplierFeed"] = relationship(back_populates="raw_items")
    run: Mapped["SupplierFeedRun"] = relationship(back_populates="raw_items")
    supplier_product: Mapped[Optional["SupplierProduct"]] = relationship()
    
    __table_args__ = (
        UniqueConstraint("run_id", "item_hash", name="uq_feed_items_raw_hash"),
        Index("idx_feed_items_raw_feed", "feed_id"),
        Index("idx_feed_items_raw_run", "run_id"),
        Index("idx_feed_items_raw_unprocessed", "feed_id", "processed", postgresql_where="processed = false"),
    )


# ============================================================================
# 8. SUPPLIER PRODUCTS
# ============================================================================

class SupplierProduct(TimestampMixin, Base):
    __tablename__ = "supplier_products"
    # Note: updated_at uses onupdate, not created_at trigger
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False)
    supplier_sku: Mapped[str] = mapped_column(String(100), nullable=False)
    ean: Mapped[Optional[str]] = mapped_column(String(255))  # Can be compound string
    manufacturer_sku: Mapped[Optional[str]] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    brand: Mapped[Optional[str]] = mapped_column(String(100))
    category: Mapped[Optional[str]] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)
    images: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    supplier_group_code: Mapped[Optional[str]] = mapped_column(String(100))
    purchase_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))
    purchase_currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)
    recommended_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))
    supplier_availability_code_raw: Mapped[Optional[str]] = mapped_column(String(50))
    supplier_availability_text: Mapped[Optional[str]] = mapped_column(String(255))
    stock_qty: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 3))
    lead_time_days: Mapped[Optional[int]] = mapped_column(Integer)
    expected_date: Mapped[Optional[date]] = mapped_column(Date)
    weight_g: Mapped[Optional[int]] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_discontinued: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source_feed_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("supplier_feeds.id", ondelete="SET NULL"))
    last_seen_run_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("supplier_feed_runs.id", ondelete="SET NULL"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    
    # Relationships
    supplier: Mapped["Supplier"] = relationship(back_populates="supplier_products")
    source_feed: Mapped[Optional["SupplierFeed"]] = relationship(back_populates="supplier_products")
    supply_sources: Mapped[List["ProductSupplySource"]] = relationship(back_populates="supplier_product")
    
    __table_args__ = (
        UniqueConstraint("supplier_id", "supplier_sku", name="uq_supplier_products_sku"),
        Index("idx_supplier_products_ean", "ean", postgresql_where="ean IS NOT NULL"),
        Index("idx_supplier_products_supplier", "supplier_id"),
        Index("idx_supplier_products_active", "supplier_id", "is_active", postgresql_where="is_active = true"),
    )


# ============================================================================
# 9. PRODUCT GROUPS
# ============================================================================

class ProductGroup(TimestampMixin, Base):
    __tablename__ = "product_groups"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    brand: Mapped[Optional[str]] = mapped_column(String(100))
    category: Mapped[Optional[str]] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)
    main_image_url: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    
    # Relationships
    products: Mapped[List["Product"]] = relationship(back_populates="group")
    
    __table_args__ = (
        Index("idx_product_groups_brand", "brand", postgresql_where="brand IS NOT NULL"),
    )


# ============================================================================
# 10. PRODUCTS (NO primary_ean column - use product_identifiers)
# ============================================================================

class Product(TimestampMixin, Base):
    __tablename__ = "products"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sku: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    supplier_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("suppliers.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    brand: Mapped[Optional[str]] = mapped_column(String(100))
    category: Mapped[Optional[str]] = mapped_column(String(255))
    weight_g: Mapped[Optional[int]] = mapped_column(Integer)
    group_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("product_groups.id", ondelete="SET NULL"))
    supplier_availability: Mapped[AvailabilityStatus] = mapped_column(
        SQLEnum(AvailabilityStatus, name="availability_status"),
        default=AvailabilityStatus.unknown,
        nullable=False
    )
    supplier_stock: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 3))
    supplier_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))
    supplier_feed_data: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    supplier_feed_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    validation_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    validation_reason: Mapped[Optional[str]] = mapped_column(String(255))
    created_from_source: Mapped[Optional[str]] = mapped_column(String(50))
    source_supplier_product_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("supplier_products.id", ondelete="SET NULL")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    
    # Relationships
    supplier: Mapped[Optional["Supplier"]] = relationship(back_populates="products")
    group: Mapped[Optional["ProductGroup"]] = relationship(back_populates="products")
    identifiers: Mapped[List["ProductIdentifier"]] = relationship(
        back_populates="product", 
        cascade="all, delete-orphan"
    )
    stock_balances: Mapped[List["StockBalance"]] = relationship(back_populates="product")
    stock_movements: Mapped[List["StockMovement"]] = relationship(back_populates="product")
    supply_sources: Mapped[List["ProductSupplySource"]] = relationship(back_populates="product")
    
    __table_args__ = (
        Index("idx_products_group", "group_id", postgresql_where="group_id IS NOT NULL"),
        Index("idx_products_active", "is_active", postgresql_where="is_active = true"),
        Index("idx_products_validation", "validation_required", postgresql_where="validation_required = true"),
        Index("idx_products_supplier", "supplier_id", postgresql_where="supplier_id IS NOT NULL"),
    )
    
    @hybrid_property
    def primary_ean(self) -> Optional[str]:
        """Get primary barcode (EAN/UPC/unverified) - for compatibility."""
        for ident in self.identifiers:
            if ident.is_primary and ident.identifier_type in (
                IdentifierType.ean, IdentifierType.upc, IdentifierType.unverified_barcode
            ):
                return ident.value
        return None


# ============================================================================
# 11. PRODUCT IDENTIFIERS (Multi-EAN support)
# ============================================================================

class ProductIdentifier(Base):
    __tablename__ = "product_identifiers"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    identifier_type: Mapped[IdentifierType] = mapped_column(
        SQLEnum(IdentifierType, name="identifier_type"),
        nullable=False
    )
    value: Mapped[str] = mapped_column(String(100), nullable=False)
    supplier_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("suppliers.id", ondelete="CASCADE"))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    
    # Relationships
    product: Mapped["Product"] = relationship(back_populates="identifiers")
    supplier: Mapped[Optional["Supplier"]] = relationship()
    
    __table_args__ = (
        # supplier_sku requires supplier_id
        CheckConstraint(
            "identifier_type != 'supplier_sku' OR supplier_id IS NOT NULL",
            name="chk_supplier_sku_has_supplier"
        ),
        # Fast lookup by value
        Index("idx_product_identifiers_value", "value"),
        Index("idx_product_identifiers_product", "product_id"),
        Index("idx_product_identifiers_type_value", "identifier_type", "value"),
        # EAN: Globally unique
        Index("idx_identifiers_ean_unique", "value", unique=True, 
              postgresql_where="identifier_type = 'ean'"),
        # UPC: Globally unique
        Index("idx_identifiers_upc_unique", "value", unique=True,
              postgresql_where="identifier_type = 'upc'"),
        # unverified_barcode: Unique per product
        Index("idx_identifiers_unverified_per_product", "product_id", "value", unique=True,
              postgresql_where="identifier_type = 'unverified_barcode'"),
        # supplier_sku: Unique per supplier
        Index("idx_identifiers_supplier_sku_unique", "supplier_id", "value", unique=True,
              postgresql_where="identifier_type = 'supplier_sku'"),
        # internal_sku: Globally unique
        Index("idx_identifiers_internal_sku_unique", "value", unique=True,
              postgresql_where="identifier_type = 'internal_sku'"),
        # manufacturer/custom: NOT unique (just indexed)
        Index("idx_identifiers_manufacturer", "value",
              postgresql_where="identifier_type = 'manufacturer'"),
        Index("idx_identifiers_custom", "value",
              postgresql_where="identifier_type = 'custom'"),
        # BARCODE GROUP: max 1 primary across (ean, upc, unverified_barcode)
        Index("idx_identifiers_primary_barcode_group", "product_id", unique=True,
              postgresql_where="is_primary = true AND identifier_type IN ('ean', 'upc', 'unverified_barcode')"),
        # OTHER TYPES: max 1 primary per (product_id, identifier_type)
        Index("idx_identifiers_primary_per_type", "product_id", "identifier_type", unique=True,
              postgresql_where="is_primary = true AND identifier_type NOT IN ('ean', 'upc', 'unverified_barcode')"),
    )
