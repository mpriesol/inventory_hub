# inventory_hub/services/identifiers.py
"""
Product Identifier Service - Multi-EAN support for v12 FINAL.

Handles:
- Barcode classification (EAN-13, EAN-8, UPC, unverified_barcode, etc.)
- Compound EAN string splitting (e.g., "398828/6927116185329/6938112675813")
- Primary barcode management
- Barcode lookup
"""
from __future__ import annotations
import re
from typing import Optional, List, Tuple
from decimal import Decimal

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from inventory_hub.db_models import (
    Product, ProductIdentifier, IdentifierType, Supplier
)


class ProductIdentifierService:
    """Service for managing product identifiers with multi-EAN support."""
    
    BARCODE_TYPES = (IdentifierType.ean, IdentifierType.upc, IdentifierType.unverified_barcode)
    
    def __init__(self, db: AsyncSession):
        self.db = db
    
    # =========================================================================
    # Classification
    # =========================================================================
    
    @staticmethod
    def is_valid_ean13_checksum(code: str) -> bool:
        """Validate EAN-13 checksum."""
        if len(code) != 13 or not code.isdigit():
            return False
        total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(code[:12]))
        check = (10 - (total % 10)) % 10
        return int(code[12]) == check
    
    @staticmethod
    def is_valid_ean8_checksum(code: str) -> bool:
        """Validate EAN-8 checksum."""
        if len(code) != 8 or not code.isdigit():
            return False
        total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(code[:7]))
        check = (10 - (total % 10)) % 10
        return int(code[7]) == check
    
    @staticmethod
    def is_valid_upc_checksum(code: str) -> bool:
        """Validate UPC-A checksum."""
        if len(code) != 12 or not code.isdigit():
            return False
        total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(code[:11]))
        check = (10 - (total % 10)) % 10
        return int(code[11]) == check
    
    @classmethod
    def classify_barcode(cls, code: str) -> IdentifierType:
        """
        Classify barcode by pattern and checksum.
        
        Returns:
            IdentifierType: ean, upc, unverified_barcode, or custom
        """
        code = (code or "").strip()
        if not code:
            return IdentifierType.custom
        
        # EAN-13 (13 digits with valid checksum)
        if re.match(r'^\d{13}$', code) and cls.is_valid_ean13_checksum(code):
            return IdentifierType.ean
        
        # EAN-8 (8 digits with valid checksum)
        if re.match(r'^\d{8}$', code) and cls.is_valid_ean8_checksum(code):
            return IdentifierType.ean
        
        # UPC-A (12 digits with valid checksum)
        if re.match(r'^\d{12}$', code) and cls.is_valid_upc_checksum(code):
            return IdentifierType.upc
        
        # Unverified barcode (4-10 numeric digits without valid checksum)
        if re.match(r'^\d{4,10}$', code):
            return IdentifierType.unverified_barcode
        
        # Default to custom
        return IdentifierType.custom
    
    @classmethod
    def split_compound_ean(cls, ean_string: str) -> List[Tuple[str, IdentifierType]]:
        """
        Split compound EAN string into individual codes with types.
        
        Example:
            "398828/6927116185329/6938112675813" -> [
                ("398828", IdentifierType.unverified_barcode),
                ("6927116185329", IdentifierType.ean),
                ("6938112675813", IdentifierType.ean),
            ]
        
        Args:
            ean_string: Compound string with /,; or space delimiters
            
        Returns:
            List of (code, identifier_type) tuples
        """
        if not ean_string:
            return []
        
        # Split by common delimiters
        codes = re.split(r'[/,;\s]+', ean_string)
        result = []
        
        for code in codes:
            code = code.strip()
            if not code:
                continue
            
            id_type = cls.classify_barcode(code)
            result.append((code, id_type))
        
        return result
    
    # =========================================================================
    # CRUD Operations
    # =========================================================================
    
    async def add_identifier(
        self,
        product_id: int,
        value: str,
        identifier_type: Optional[IdentifierType] = None,
        supplier_id: Optional[int] = None,
        is_primary: bool = False,
        notes: Optional[str] = None,
    ) -> ProductIdentifier:
        """
        Add identifier to product.
        
        If is_primary=True and identifier_type is in BARCODE_TYPES,
        clears existing primary in barcode group first.
        """
        value = (value or "").strip()
        if not value:
            raise ValueError("Identifier value cannot be empty")
        
        # Auto-classify if type not provided
        if identifier_type is None:
            identifier_type = self.classify_barcode(value)
        
        # For supplier_sku, supplier_id is required
        if identifier_type == IdentifierType.supplier_sku and supplier_id is None:
            raise ValueError("supplier_id is required for supplier_sku type")
        
        # Clear existing primary in barcode group if needed
        if is_primary and identifier_type in self.BARCODE_TYPES:
            await self._clear_barcode_group_primary(product_id)
        elif is_primary:
            await self._clear_type_primary(product_id, identifier_type)
        
        identifier = ProductIdentifier(
            product_id=product_id,
            identifier_type=identifier_type,
            value=value,
            supplier_id=supplier_id,
            is_primary=is_primary,
            notes=notes,
        )
        self.db.add(identifier)
        await self.db.flush()
        return identifier
    
    async def add_compound_ean(
        self,
        product_id: int,
        ean_string: str,
        set_first_as_primary: bool = True,
    ) -> List[ProductIdentifier]:
        """
        Split compound EAN string and add all codes to product.
        
        Args:
            product_id: Target product
            ean_string: Compound string like "398828/6927116185329/6938112675813"
            set_first_as_primary: If True, first valid EAN is set as primary
            
        Returns:
            List of created ProductIdentifier objects
        """
        codes = self.split_compound_ean(ean_string)
        if not codes:
            return []
        
        # Sort by priority for primary selection (EAN > UPC > unverified)
        def priority(item):
            _, id_type = item
            if id_type == IdentifierType.ean:
                return 0
            elif id_type == IdentifierType.upc:
                return 1
            elif id_type == IdentifierType.unverified_barcode:
                return 2
            return 3
        
        sorted_codes = sorted(codes, key=priority)
        
        identifiers = []
        primary_set = False
        
        for code, id_type in sorted_codes:
            is_primary = set_first_as_primary and not primary_set and id_type in self.BARCODE_TYPES
            
            try:
                identifier = await self.add_identifier(
                    product_id=product_id,
                    value=code,
                    identifier_type=id_type,
                    is_primary=is_primary,
                )
                identifiers.append(identifier)
                if is_primary:
                    primary_set = True
            except Exception:
                # Skip duplicates
                continue
        
        return identifiers
    
    async def _clear_barcode_group_primary(self, product_id: int) -> None:
        """Clear primary flag for barcode group (ean, upc, unverified_barcode)."""
        stmt = (
            select(ProductIdentifier)
            .where(
                ProductIdentifier.product_id == product_id,
                ProductIdentifier.is_primary == True,
                ProductIdentifier.identifier_type.in_(self.BARCODE_TYPES),
            )
        )
        result = await self.db.execute(stmt)
        for ident in result.scalars():
            ident.is_primary = False
    
    async def _clear_type_primary(self, product_id: int, identifier_type: IdentifierType) -> None:
        """Clear primary flag for specific identifier type."""
        stmt = (
            select(ProductIdentifier)
            .where(
                ProductIdentifier.product_id == product_id,
                ProductIdentifier.is_primary == True,
                ProductIdentifier.identifier_type == identifier_type,
            )
        )
        result = await self.db.execute(stmt)
        for ident in result.scalars():
            ident.is_primary = False
    
    # =========================================================================
    # Lookup
    # =========================================================================
    
    async def find_product_by_barcode(self, code: str) -> Optional[Product]:
        """
        Find active product by any barcode (EAN/UPC/unverified).
        
        This is the main lookup for scanner operations.
        """
        code = (code or "").strip()
        if not code:
            return None
        
        stmt = (
            select(Product)
            .join(ProductIdentifier)
            .where(
                ProductIdentifier.value == code,
                ProductIdentifier.identifier_type.in_(self.BARCODE_TYPES),
                Product.is_active == True,
            )
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()
    
    async def find_product_by_identifier(
        self,
        value: str,
        identifier_type: Optional[IdentifierType] = None,
        supplier_id: Optional[int] = None,
    ) -> Optional[Product]:
        """
        Find product by any identifier with optional type filter.
        """
        value = (value or "").strip()
        if not value:
            return None
        
        conditions = [
            ProductIdentifier.value == value,
            Product.is_active == True,
        ]
        
        if identifier_type:
            conditions.append(ProductIdentifier.identifier_type == identifier_type)
        
        if supplier_id and identifier_type == IdentifierType.supplier_sku:
            conditions.append(ProductIdentifier.supplier_id == supplier_id)
        
        stmt = (
            select(Product)
            .join(ProductIdentifier)
            .where(and_(*conditions))
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()
    
    async def get_all_barcodes(self, product_id: int) -> List[str]:
        """Get all barcode values for product."""
        stmt = (
            select(ProductIdentifier.value)
            .where(
                ProductIdentifier.product_id == product_id,
                ProductIdentifier.identifier_type.in_(self.BARCODE_TYPES),
            )
            .order_by(ProductIdentifier.is_primary.desc(), ProductIdentifier.id)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars())
    
    async def get_primary_barcode(self, product_id: int) -> Optional[str]:
        """Get primary barcode for product (from barcode group)."""
        stmt = (
            select(ProductIdentifier.value)
            .where(
                ProductIdentifier.product_id == product_id,
                ProductIdentifier.identifier_type.in_(self.BARCODE_TYPES),
                ProductIdentifier.is_primary == True,
            )
            .limit(1)
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()
