"""Shared scanner identity checks; quantity and operation recording stay with callers."""
from fastapi import HTTPException
from inventory_hub.services.identifiers import ProductIdentifierService
from inventory_hub.services.product_identity import RemoteIdentity, load_identity_index, verified_barcodes
from inventory_hub.services.receiving_barcodes import line_barcode_display


async def match_scan(db, session, code, prefix, *, line_id=None, resolve_line):
    identifier_service = ProductIdentifierService(db)
    # A scan can name a listed invoice barcode, a supplier code or a canonical
    # prefixed SKU. Multiple invoice lines need an explicit selection because
    # their prices may differ even when the physical product is identical.
    lines = sorted(session.lines, key=lambda line: line.line_number)
    candidates = [line for line in lines if
        code in {value for value, _kind in identifier_service.split_compound_ean(line.ean or "")}
        or (line.supplier_sku and code in (line.supplier_sku.strip(), f"{prefix}{line.supplier_sku.strip()}"))]
    # Also allow an alternative registered EAN for an already assigned product.
    scan_identity = RemoteIdentity(0, code, barcodes=(code,),
        supplier_id=session.supplier_id, supplier_sku=code)
    index = await load_identity_index(db, 0, [scan_identity], local=True)
    scan_resolution = index.resolve(scan_identity)
    if scan_resolution.status == "conflict":
        raise HTTPException(409, detail={"code": "receiving_identity_conflict",
            "message": "Naskenovaný kód označuje viac produktov. Skontroluj identifikátory.",
            "reasons": scan_resolution.reasons, "candidate_product_ids": scan_resolution.candidate_product_ids})
    if scan_resolution.product_id is not None:
        for line in lines:
            if line.product_id == scan_resolution.product_id and line not in candidates:
                candidates.append(line)
    if not candidates:
        displayed = await line_barcode_display(db, session.supplier_id, lines, prefix)
        candidates = [line for line in lines if code in verified_barcodes((displayed[line.id]["display_ean"],))]
    if line_id is not None:
        matched_line = next((line for line in candidates if line.id == line_id), None)
        if matched_line is None:
            raise HTTPException(409, detail={"code": "scan_line_mismatch",
                "message": "Vybraný riadok nepatrí k tomuto kódu a príjmu."})
    elif len(candidates) > 1:
        raise HTTPException(409, detail={"code": "scan_line_ambiguous",
            "message": "Kód zodpovedá viacerým riadkom. Vyber konkrétny riadok alebo uprav jeho množstvo ručne.",
            "line_numbers": [line.line_number for line in candidates], "line_ids": [line.id for line in candidates]})
    else:
        matched_line = candidates[0] if candidates else None
    product = index.products.get(scan_resolution.product_id)
    if matched_line is not None:
        line_product, matched_by = await resolve_line(db, session.supplier, matched_line, prefix)
        if product is not None and line_product is not None and product.id != line_product.id:
            raise HTTPException(409, detail={"code": "receiving_identity_conflict",
                "message": "Naskenovaný kód a faktúrový riadok označujú odlišné produkty."})
        product = line_product or product
        if product is not None:
            matched_line.product_id = product.id
            matched_line.product = product
            matched_line.match_method = matched_by or scan_resolution.matched_by
    return matched_line, product
