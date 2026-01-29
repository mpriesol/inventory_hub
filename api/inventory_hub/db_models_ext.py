# inventory_hub/db_models_ext.py
"""
SQLAlchemy ORM Models for Inventory Hub v12 FINAL - Part 2.

Continuation of db_models.py with remaining 19 tables.
"""
from __future__ import annotations
from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List
import hashlib

from sqlalchemy import (
    String, Integer, BigInteger, Boolean, Text, Date, DateTime,
    Numeric, ForeignKey, Index, CheckConstraint, UniqueConstraint,
    Enum as SQLEnum, Computed, func
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import JSONB, ARRAY

from inventory_hub.database import Base
from inventory_hub.db_models import (
    TimestampMixin, 
    AvailabilityStatus, MovementType, ReceivingStatus, 
    InventoryCountStatus, InventoryCountType, IdentifierType,
    OrderStatus, ReservationStatus, OversellMode,
    ScanSessionType, ScanStatus, ConfigEntityType,
    FeedRunStatus, SyncOutboxStatus, AvailabilityCode, StockPositionCode,
    Product, Warehouse, Supplier, Shop, ProductIdentifier
)


# ============================================================================
# 12. PRODUCT VARIANT ATTRIBUTES
# ============================================================================

class ProductVariantAttribute(Base):
    __tablename__ = "product_variant_attributes"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    attribute_name: Mapped[str] = mapped_column(String(100), nullable=False)
    attribute_value: Mapped[str] = mapped_column(String(255), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    
    # Relationships
    product: Mapped["Product"] = relationship()
    
    __table_args__ = (
        UniqueConstraint("product_id", "attribute_name", name="uq_variant_attrs"),
        Index("idx_variant_attrs_product", "product_id"),
    )


# ============================================================================
# 13. PRODUCT SUPPLY SOURCES
# ============================================================================

class ProductSupplySource(TimestampMixin, Base):
    __tablename__ = "product_supply_sources"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    supplier_product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("supplier_products.id", ondelete="CASCADE"), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    default_lead_time_days: Mapped[Optional[int]] = mapped_column(Integer)
    orderable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    min_order_qty: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    pack_size: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    negotiated_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))
    negotiated_currency: Mapped[Optional[str]] = mapped_column(String(3))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    
    # Relationships
    product: Mapped["Product"] = relationship(back_populates="supply_sources")
    supplier_product: Mapped["SupplierProduct"] = relationship(back_populates="supply_sources")
    
    __table_args__ = (
        UniqueConstraint("product_id", "supplier_product_id", name="uq_supply_sources"),
        Index("idx_supply_sources_primary", "product_id", unique=True,
              postgresql_where="is_primary = true"),
        Index("idx_supply_sources_product", "product_id"),
    )


# Import SupplierProduct for type hints
from inventory_hub.db_models import SupplierProduct


# ============================================================================
# 14. STOCK BALANCES
# ============================================================================

class StockBalance(TimestampMixin, Base):
    __tablename__ = "stock_balances"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    qty_on_hand: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0, nullable=False)
    qty_reserved: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0, nullable=False)
    # qty_available is GENERATED in PostgreSQL, but we compute it here too
    min_quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0, nullable=False)
    avg_cost: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=0, nullable=False)
    total_value: Mapped[Decimal] = mapped_column(Numeric(16, 4), default=0, nullable=False)
    last_purchase_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))
    last_purchase_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_movement_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_movement_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("stock_movements.id", ondelete="SET NULL"))
    
    # Relationships
    product: Mapped["Product"] = relationship(back_populates="stock_balances")
    warehouse: Mapped["Warehouse"] = relationship(back_populates="stock_balances")
    
    @property
    def qty_available(self) -> Decimal:
        """Computed available quantity."""
        return self.qty_on_hand - self.qty_reserved
    
    __table_args__ = (
        UniqueConstraint("product_id", "warehouse_id", name="uq_stock_balances"),
        CheckConstraint("qty_reserved >= 0", name="chk_qty_reserved_non_negative"),
        CheckConstraint("avg_cost >= 0", name="chk_avg_cost_non_negative"),
        Index("idx_stock_balances_product", "product_id"),
        Index("idx_stock_balances_warehouse", "warehouse_id"),
    )


# ============================================================================
# 15. STOCK MOVEMENTS (IMMUTABLE LEDGER)
# ============================================================================

class StockMovement(Base):
    __tablename__ = "stock_movements"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    movement_type: Mapped[MovementType] = mapped_column(
        SQLEnum(MovementType, name="movement_type"),
        nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit_cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))
    unit_cost_original: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))
    unit_cost_currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)
    fx_rate_to_eur: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=1.0, nullable=False)
    reference_type: Mapped[Optional[str]] = mapped_column(String(50))
    reference_id: Mapped[Optional[str]] = mapped_column(String(100))
    reference_source: Mapped[Optional[str]] = mapped_column(String(100))
    balance_after: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    avg_cost_after: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(100), default="system", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    
    # Relationships
    product: Mapped["Product"] = relationship(back_populates="stock_movements")
    warehouse: Mapped["Warehouse"] = relationship(back_populates="stock_movements")
    
    __table_args__ = (
        CheckConstraint("quantity != 0", name="chk_quantity_not_zero"),
        CheckConstraint("fx_rate_to_eur > 0", name="chk_fx_rate_positive"),
        Index("idx_movements_product", "product_id"),
        Index("idx_movements_warehouse", "warehouse_id"),
        Index("idx_movements_type", "movement_type"),
        Index("idx_movements_created", "created_at"),
        Index("idx_movements_reference", "reference_type", "reference_id"),
    )


# ============================================================================
# 16. FX RATES
# ============================================================================

class FxRate(Base):
    __tablename__ = "fx_rates"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    from_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    to_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    
    __table_args__ = (
        UniqueConstraint("from_currency", "to_currency", "rate_date", name="uq_fx_rates"),
        CheckConstraint("rate > 0", name="chk_rate_positive"),
        Index("idx_fx_rates_lookup", "from_currency", "to_currency", "rate_date"),
    )


# ============================================================================
# 17. AVAILABILITY PROFILES
# ============================================================================

class AvailabilityProfile(TimestampMixin, Base):
    __tablename__ = "availability_profiles"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    in_stock_min_qty: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    
    # Labels
    label_in_stock: Mapped[str] = mapped_column(String(100), default="Skladom", nullable=False)
    label_in_stock_code: Mapped[Optional[str]] = mapped_column(String(50))
    label_low_stock: Mapped[str] = mapped_column(String(100), default="Posledné kusy", nullable=False)
    label_low_stock_code: Mapped[Optional[str]] = mapped_column(String(50))
    label_supplier_1_3_days: Mapped[str] = mapped_column(String(100), default="Do 3 dní", nullable=False)
    label_supplier_1_3_days_code: Mapped[Optional[str]] = mapped_column(String(50))
    label_supplier_3_5_days: Mapped[str] = mapped_column(String(100), default="Do 5 dní", nullable=False)
    label_supplier_3_5_days_code: Mapped[Optional[str]] = mapped_column(String(50))
    label_supplier_1_2_weeks: Mapped[str] = mapped_column(String(100), default="Do 2 týždňov", nullable=False)
    label_supplier_1_2_weeks_code: Mapped[Optional[str]] = mapped_column(String(50))
    label_supplier_on_order: Mapped[str] = mapped_column(String(100), default="Na objednávku", nullable=False)
    label_supplier_on_order_code: Mapped[Optional[str]] = mapped_column(String(50))
    label_check_availability: Mapped[str] = mapped_column(String(100), default="Overíme dostupnosť", nullable=False)
    label_check_availability_code: Mapped[Optional[str]] = mapped_column(String(50))
    label_unavailable: Mapped[str] = mapped_column(String(100), default="Nedostupné", nullable=False)
    label_unavailable_code: Mapped[Optional[str]] = mapped_column(String(50))
    
    # Flags
    show_exact_qty: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_backorder: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    hide_when_unavailable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    prefer_local_stock: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    
    # Relationships
    shop: Mapped["Shop"] = relationship(back_populates="availability_profiles")
    
    __table_args__ = (
        UniqueConstraint("shop_id", "code", name="uq_availability_profiles"),
        Index("idx_availability_profiles_default", "shop_id", unique=True,
              postgresql_where="is_default = true"),
    )


# ============================================================================
# 18. SHOP PRODUCT AVAILABILITY
# ============================================================================

class ShopProductAvailability(Base):
    __tablename__ = "shop_product_availability"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    qty_on_hand: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0, nullable=False)
    qty_reserved: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0, nullable=False)
    qty_available: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0, nullable=False)
    best_supplier_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("suppliers.id", ondelete="SET NULL"))
    best_supplier_qty: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 3))
    best_supplier_lead_time_days: Mapped[Optional[int]] = mapped_column(Integer)
    best_supplier_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))
    computed_availability_code: Mapped[AvailabilityCode] = mapped_column(
        SQLEnum(AvailabilityCode, name="availability_code"), nullable=False
    )
    computed_availability_label: Mapped[str] = mapped_column(String(100), nullable=False)
    computed_shop_code: Mapped[Optional[str]] = mapped_column(String(50))
    stock_position: Mapped[StockPositionCode] = mapped_column(
        SQLEnum(StockPositionCode, name="stock_position_code"), nullable=False
    )
    stock_position_label: Mapped[Optional[str]] = mapped_column(String(100))
    
    # Manual override
    manual_override: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    manual_availability_code: Mapped[Optional[AvailabilityCode]] = mapped_column(
        SQLEnum(AvailabilityCode, name="availability_code")
    )
    manual_availability_label: Mapped[Optional[str]] = mapped_column(String(100))
    manual_shop_code: Mapped[Optional[str]] = mapped_column(String(50))
    manual_override_reason: Mapped[Optional[str]] = mapped_column(String(255))
    manual_override_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    manual_override_by: Mapped[Optional[str]] = mapped_column(String(100))
    manual_override_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    
    # Final values
    availability_code: Mapped[AvailabilityCode] = mapped_column(
        SQLEnum(AvailabilityCode, name="availability_code"), nullable=False
    )
    availability_label: Mapped[str] = mapped_column(String(100), nullable=False)
    availability_shop_code: Mapped[Optional[str]] = mapped_column(String(50))
    is_orderable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sync_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    
    __table_args__ = (
        UniqueConstraint("shop_id", "product_id", name="uq_shop_product_availability"),
        Index("idx_shop_availability_sync", "shop_id", "sync_required",
              postgresql_where="sync_required = true"),
        Index("idx_shop_availability_product", "product_id"),
    )


# ============================================================================
# 19. SHOP PRODUCTS
# ============================================================================

class ShopProduct(TimestampMixin, Base):
    __tablename__ = "shop_products"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    external_id: Mapped[Optional[str]] = mapped_column(String(100))
    external_code: Mapped[Optional[str]] = mapped_column(String(100))
    variant_code: Mapped[Optional[str]] = mapped_column(String(100))
    parent_code: Mapped[Optional[str]] = mapped_column(String(100))
    is_variant: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    shop_availability: Mapped[Optional[str]] = mapped_column(String(100))
    shop_stock: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 3))
    shop_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    push_pending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_push_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_push_status: Mapped[Optional[str]] = mapped_column(String(50))
    last_push_error: Mapped[Optional[str]] = mapped_column(Text)
    last_pull_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_listed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    
    # Relationships
    shop: Mapped["Shop"] = relationship(back_populates="shop_products")
    product: Mapped["Product"] = relationship()
    
    __table_args__ = (
        UniqueConstraint("shop_id", "product_id", name="uq_shop_products"),
        Index("idx_shop_products_pending", "shop_id", postgresql_where="push_pending = true"),
        Index("idx_shop_products_external", "shop_id", "external_id"),
    )


# ============================================================================
# 20. SHOP SYNC OUTBOX
# ============================================================================

class ShopSyncOutbox(TimestampMixin, Base):
    __tablename__ = "shop_sync_outbox"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    sync_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[SyncOutboxStatus] = mapped_column(
        SQLEnum(SyncOutboxStatus, name="sync_outbox_status"),
        default=SyncOutboxStatus.pending,
        nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    response_data: Mapped[Optional[dict]] = mapped_column(JSONB)
    
    __table_args__ = (
        Index("idx_sync_outbox_pending", "next_attempt_at",
              postgresql_where="status IN ('pending', 'failed')"),
        Index("idx_sync_outbox_shop", "shop_id", "status"),
    )


# ============================================================================
# 21. SHOP ORDERS
# ============================================================================

class ShopOrder(TimestampMixin, Base):
    __tablename__ = "shop_orders"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    external_id: Mapped[str] = mapped_column(String(100), nullable=False)
    external_code: Mapped[Optional[str]] = mapped_column(String(100))
    order_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        SQLEnum(OrderStatus, name="order_status"),
        default=OrderStatus.new,
        nullable=False
    )
    customer_id: Mapped[Optional[str]] = mapped_column(String(100))
    shipping_country: Mapped[Optional[str]] = mapped_column(String(3))
    shipping_method: Mapped[Optional[str]] = mapped_column(String(100))
    payment_method: Mapped[Optional[str]] = mapped_column(String(100))
    subtotal: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    shipping_cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)
    
    # Relationships
    shop: Mapped["Shop"] = relationship(back_populates="shop_orders")
    items: Mapped[List["ShopOrderItem"]] = relationship(back_populates="order")
    
    __table_args__ = (
        UniqueConstraint("shop_id", "external_id", name="uq_shop_orders"),
        Index("idx_shop_orders_date", "order_date"),
        Index("idx_shop_orders_status", "shop_id", "status"),
    )


# ============================================================================
# 22. SHOP ORDER ITEMS
# ============================================================================

class ShopOrderItem(TimestampMixin, Base):
    __tablename__ = "shop_order_items"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shop_orders.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="SET NULL"))
    external_item_id: Mapped[Optional[str]] = mapped_column(String(100))
    external_product_code: Mapped[Optional[str]] = mapped_column(String(100))
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    total_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    
    # Relationships
    order: Mapped["ShopOrder"] = relationship(back_populates="items")
    product: Mapped[Optional["Product"]] = relationship()
    reservation: Mapped[Optional["Reservation"]] = relationship(back_populates="order_item")
    
    __table_args__ = (
        CheckConstraint("quantity > 0", name="chk_order_item_qty_positive"),
        Index("idx_shop_order_items_order", "order_id"),
        Index("idx_shop_order_items_product", "product_id"),
    )


# ============================================================================
# 23. RESERVATIONS
# ============================================================================

class Reservation(TimestampMixin, Base):
    __tablename__ = "reservations"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shop_order_item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("shop_order_items.id", ondelete="CASCADE"), unique=True, nullable=False)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    shortage_qty: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0, nullable=False)
    status: Mapped[ReservationStatus] = mapped_column(
        SQLEnum(ReservationStatus, name="reservation_status"),
        default=ReservationStatus.reserved,
        nullable=False
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    sale_movement_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("stock_movements.id", ondelete="SET NULL"))
    
    # Relationships
    order_item: Mapped["ShopOrderItem"] = relationship(back_populates="reservation")
    product: Mapped["Product"] = relationship()
    warehouse: Mapped["Warehouse"] = relationship()
    
    __table_args__ = (
        UniqueConstraint("shop_order_item_id", name="uq_reservations_order_item"),
        CheckConstraint("quantity > 0", name="chk_reservation_qty_positive"),
        CheckConstraint("shortage_qty >= 0", name="chk_shortage_non_negative"),
        Index("idx_reservations_product", "product_id"),
        Index("idx_reservations_active", "status",
              postgresql_where="status IN ('reserved', 'backorder')"),
    )


# ============================================================================
# 24. RECEIVING SESSIONS
# ============================================================================

class ReceivingSession(TimestampMixin, Base):

# ============================================================================
# 24. RECEIVING SESSIONS (Extended with Invoice Management fields)
# ============================================================================

class ReceivingSession(TimestampMixin, Base):
    __tablename__ = "receiving_sessions"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    invoice_number: Mapped[str] = mapped_column(String(100), nullable=False)
    invoice_date: Mapped[Optional[date]] = mapped_column(Date)
    invoice_file_path: Mapped[Optional[str]] = mapped_column(Text)
    invoice_currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)
    fx_rate_to_eur: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=1.0, nullable=False)
    fx_rate_source: Mapped[Optional[str]] = mapped_column(String(50))
    fx_rate_date: Mapped[Optional[date]] = mapped_column(Date)
    source_hash: Mapped[Optional[str]] = mapped_column(String(64))
    import_source: Mapped[Optional[str]] = mapped_column(String(50))
    total_lines: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    status: Mapped[ReceivingStatus] = mapped_column(
        SQLEnum(ReceivingStatus, name="receiving_status"),
        default=ReceivingStatus.new,
        nullable=False
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    paused_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    started_by: Mapped[Optional[str]] = mapped_column(String(100))
    finished_by: Mapped[Optional[str]] = mapped_column(String(100))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    session_data: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    
    # === Invoice Management Extension (from 002_invoice_management.sql) ===
    # Payment tracking
    payment_status: Mapped[str] = mapped_column(String(20), default="unpaid", nullable=False)
    due_date: Mapped[Optional[date]] = mapped_column(Date)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    paid_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    
    # VAT handling
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("23.00"), nullable=False)
    vat_included: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    total_without_vat: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    computed_vat: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    total_with_vat: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    
    # Supplier info cache
    supplier_country: Mapped[Optional[str]] = mapped_column(String(2))
    
    # Relationships
    supplier: Mapped["Supplier"] = relationship(back_populates="receiving_sessions")
    warehouse: Mapped["Warehouse"] = relationship(back_populates="receiving_sessions")
    lines: Mapped[List["ReceivingLine"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    scan_events: Mapped[List["ScanEvent"]] = relationship(back_populates="receiving_session")
    
    __table_args__ = (
        UniqueConstraint("supplier_id", "invoice_number", name="uq_receiving_invoice"),
        Index("idx_receiving_source_hash", "supplier_id", "source_hash", unique=True,
              postgresql_where="source_hash IS NOT NULL"),
        Index("idx_receiving_status", "status"),
        Index("idx_receiving_sessions_payment", "payment_status"),
        Index("idx_receiving_sessions_due_date", "due_date"),
    )


# ============================================================================
# 25. RECEIVING LINES (Extended with Invoice Management fields)
# ============================================================================

class ReceivingLine(TimestampMixin, Base):
    __tablename__ = "receiving_lines"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("receiving_sessions.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="SET NULL"))
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)  # v12 FINAL: NOT NULL
    supplier_sku: Mapped[Optional[str]] = mapped_column(String(100))
    ean: Mapped[Optional[str]] = mapped_column(String(20))
    description: Mapped[Optional[str]] = mapped_column(Text)
    ordered_qty: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    received_qty: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0, nullable=False)
    unit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))
    total_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    unit_price_original: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))
    total_price_original: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    match_method: Mapped[Optional[str]] = mapped_column(String(50))
    
    # === Invoice Management Extension (from 002_invoice_management.sql) ===
    # VAT per line
    vat_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    unit_price_with_vat: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4))
    total_price_with_vat: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    
    # Product matching
    is_new_product: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    matched_supplier_product_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("supplier_products.id", ondelete="SET NULL"))
    
    # Product image caching
    product_image_url: Mapped[Optional[str]] = mapped_column(Text)
    product_image_cached_path: Mapped[Optional[str]] = mapped_column(Text)
    
    # Relationships
    session: Mapped["ReceivingSession"] = relationship(back_populates="lines")
    product: Mapped[Optional["Product"]] = relationship()
    matched_supplier_product: Mapped[Optional["SupplierProduct"]] = relationship()
    scan_events: Mapped[List["ScanEvent"]] = relationship(back_populates="receiving_line")
    
    @property
    def line_fingerprint(self) -> str:
        """Compute fingerprint for debugging (matches PostgreSQL GENERATED)."""
        data = f"{self.ean or ''}|{self.supplier_sku or ''}|{self.ordered_qty}|{self.unit_price or ''}"
        return hashlib.md5(data.encode()).hexdigest()
    
    __table_args__ = (
        CheckConstraint("ordered_qty > 0", name="chk_ordered_qty_positive"),
        CheckConstraint("received_qty >= 0", name="chk_received_qty_non_negative"),
        CheckConstraint("line_number > 0", name="chk_line_number_positive"),
        UniqueConstraint("session_id", "line_number", name="idx_receiving_lines_session_line"),
        Index("idx_receiving_lines_session", "session_id"),
        Index("idx_receiving_lines_product", "product_id"),
        Index("idx_receiving_lines_ean", "ean", postgresql_where="ean IS NOT NULL"),
        Index("idx_receiving_lines_supplier_sku", "supplier_sku", postgresql_where="supplier_sku IS NOT NULL"),
        Index("idx_receiving_lines_new_product", "session_id", postgresql_where="is_new_product = true"),
        Index("idx_receiving_lines_supplier_product", "matched_supplier_product_id", 
              postgresql_where="matched_supplier_product_id IS NOT NULL"),
    )


# ============================================================================
# 26. INVENTORY COUNTS
# ============================================================================

class InventoryCount(TimestampMixin, Base):
    __tablename__ = "inventory_counts"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False)
    count_type: Mapped[InventoryCountType] = mapped_column(
        SQLEnum(InventoryCountType, name="inventory_count_type"),
        default=InventoryCountType.full,
        nullable=False
    )
    name: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[InventoryCountStatus] = mapped_column(
        SQLEnum(InventoryCountStatus, name="inventory_count_status"),
        default=InventoryCountStatus.draft,
        nullable=False
    )
    include_movements_since: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[Optional[str]] = mapped_column(String(100))
    started_by: Mapped[Optional[str]] = mapped_column(String(100))
    finished_by: Mapped[Optional[str]] = mapped_column(String(100))
    total_products: Mapped[Optional[int]] = mapped_column(Integer)
    products_counted: Mapped[Optional[int]] = mapped_column(Integer)
    products_with_variance: Mapped[Optional[int]] = mapped_column(Integer)
    total_variance_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    
    # Relationships
    warehouse: Mapped["Warehouse"] = relationship()
    lines: Mapped[List["InventoryCountLine"]] = relationship(back_populates="count")
    scan_events: Mapped[List["ScanEvent"]] = relationship(back_populates="inventory_count")
    
    __table_args__ = (
        Index("idx_inventory_counts_warehouse", "warehouse_id"),
        Index("idx_inventory_counts_status", "status"),
    )


# ============================================================================
# 27. INVENTORY COUNT LINES
# ============================================================================

class InventoryCountLine(TimestampMixin, Base):
    __tablename__ = "inventory_count_lines"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    count_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("inventory_counts.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    expected_qty: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    expected_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    counted_qty: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 3))
    counted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    counted_by: Mapped[Optional[str]] = mapped_column(String(100))
    variance_qty: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 3))
    variance_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String(50), default="pending", nullable=False)
    adjustment_movement_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("stock_movements.id", ondelete="SET NULL"))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    
    # Relationships
    count: Mapped["InventoryCount"] = relationship(back_populates="lines")
    product: Mapped["Product"] = relationship()
    scan_events: Mapped[List["ScanEvent"]] = relationship(back_populates="inventory_count_line")
    
    __table_args__ = (
        UniqueConstraint("count_id", "product_id", name="uq_inventory_count_lines"),
        Index("idx_inventory_count_lines_count", "count_id"),
    )


# ============================================================================
# 28. SCAN EVENTS
# ============================================================================

class ScanEvent(Base):
    __tablename__ = "scan_events"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_type: Mapped[ScanSessionType] = mapped_column(
        SQLEnum(ScanSessionType, name="scan_session_type"), nullable=False
    )
    receiving_session_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("receiving_sessions.id", ondelete="CASCADE"))
    receiving_line_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("receiving_lines.id", ondelete="SET NULL"))
    inventory_count_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("inventory_counts.id", ondelete="CASCADE"))
    inventory_count_line_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("inventory_count_lines.id", ondelete="SET NULL"))
    scanned_code: Mapped[str] = mapped_column(String(100), nullable=False)
    scanned_code_type: Mapped[Optional[IdentifierType]] = mapped_column(
        SQLEnum(IdentifierType, name="identifier_type")
    )
    product_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("products.id", ondelete="SET NULL"))
    matched_identifier_id: Mapped[Optional[int]] = mapped_column(BigInteger, ForeignKey("product_identifiers.id", ondelete="SET NULL"))
    match_method: Mapped[Optional[str]] = mapped_column(String(50))
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=1, nullable=False)
    status: Mapped[ScanStatus] = mapped_column(
        SQLEnum(ScanStatus, name="scan_status"),
        default=ScanStatus.active,
        nullable=False
    )
    undone_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    undone_by: Mapped[Optional[str]] = mapped_column(String(100))
    undo_reason: Mapped[Optional[str]] = mapped_column(String(255))
    scanned_by: Mapped[str] = mapped_column(String(100), nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    device_id: Mapped[Optional[str]] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    
    # Relationships
    receiving_session: Mapped[Optional["ReceivingSession"]] = relationship(back_populates="scan_events")
    receiving_line: Mapped[Optional["ReceivingLine"]] = relationship(back_populates="scan_events")
    inventory_count: Mapped[Optional["InventoryCount"]] = relationship(back_populates="scan_events")
    inventory_count_line: Mapped[Optional["InventoryCountLine"]] = relationship(back_populates="scan_events")
    product: Mapped[Optional["Product"]] = relationship()
    matched_identifier: Mapped[Optional["ProductIdentifier"]] = relationship()
    
    __table_args__ = (
        CheckConstraint(
            """(session_type = 'receiving' AND receiving_session_id IS NOT NULL AND inventory_count_id IS NULL)
            OR (session_type = 'inventory' AND inventory_count_id IS NOT NULL AND receiving_session_id IS NULL)
            OR (session_type IN ('lookup', 'adjustment') AND receiving_session_id IS NULL AND inventory_count_id IS NULL)""",
            name="chk_scan_session_fks"
        ),
        Index("idx_scan_events_receiving", "receiving_session_id",
              postgresql_where="receiving_session_id IS NOT NULL"),
        Index("idx_scan_events_inventory", "inventory_count_id",
              postgresql_where="inventory_count_id IS NOT NULL"),
        Index("idx_scan_events_code", "scanned_code"),
        Index("idx_scan_events_recent", "scanned_at"),
    )


# ============================================================================
# 29. CONFIG VERSIONS
# ============================================================================

class ConfigVersion(Base):
    __tablename__ = "config_versions"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_type: Mapped[ConfigEntityType] = mapped_column(
        SQLEnum(ConfigEntityType, name="config_entity_type"), nullable=False
    )
    entity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    entity_code: Mapped[str] = mapped_column(String(50), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    change_type: Mapped[str] = mapped_column(String(50), nullable=False)
    changed_fields: Mapped[Optional[list]] = mapped_column(ARRAY(Text))
    changed_by: Mapped[str] = mapped_column(String(100), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    change_reason: Mapped[Optional[str]] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    
    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id", "version", name="uq_config_versions"),
        Index("idx_config_versions_entity", "entity_type", "entity_id"),
        # Single current config per entity
        Index("idx_config_versions_single_current", "entity_type", "entity_id", unique=True,
              postgresql_where="is_current = true"),
    )


# ============================================================================
# 30. SYNC LOG
# ============================================================================

class SyncLog(Base):
    __tablename__ = "sync_log"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sync_type: Mapped[str] = mapped_column(String(50), nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    target_type: Mapped[Optional[str]] = mapped_column(String(50))
    target_code: Mapped[Optional[str]] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(50), default="started", nullable=False)
    items_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_success: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    items_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    error_details: Mapped[Optional[dict]] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now(), nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    
    __table_args__ = (
        Index("idx_sync_log_type", "sync_type", "target_code"),
        Index("idx_sync_log_started", "started_at"),
    )
