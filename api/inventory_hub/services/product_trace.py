"""Exact-code product investigation. Read-only: invoices and stock are evidence, not writers."""
from datetime import date
from decimal import Decimal
from urllib.parse import quote

from fastapi import HTTPException
from sqlalchemy import text
from inventory_hub.services.stock_history import list_movements

PAGE_SIZE = 50


def serial(value):
    if isinstance(value, Decimal):
        return format(value, 'f')
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return value


def as_dict(row):
    return {key: serial(value) for key, value in row.items()}


async def rows(db, sql, params):
    return [as_dict(row) for row in (await db.execute(text(sql), params)).mappings().all()]


async def trace(db, *, code='', product_id=None, invoice_page=1, movement_page=1):
    code = code.strip()
    if not code and product_id is None:
        raise HTTPException(422, detail={'code': 'code_required', 'message': 'Scan an EAN or enter a product code.'})
    params = {'code': code, 'product_id': product_id}
    matches = await rows(db, """
        SELECT DISTINCT p.id, p.sku, COALESCE(o.data #>> '{common,name}', p.name) AS name,
               p.item_type, p.stock_unit
        FROM products p LEFT JOIN product_editor_overrides o ON o.product_id = p.id
        WHERE (CAST(:code AS text) <> '' AND (p.sku = CAST(:code AS text) OR EXISTS (
            SELECT 1 FROM product_identifiers i WHERE i.product_id = p.id AND i.value = CAST(:code AS text)
            AND i.identifier_type IN ('ean','upc','unverified_barcode','internal_sku'))
            OR EXISTS (SELECT 1 FROM uploaded_invoice_lines l
                WHERE l.matched_product_id = p.id AND l.barcode_values @> ARRAY[CAST(:code AS text)])))
           OR (CAST(:code AS text) = '' AND p.id = :product_id)
        ORDER BY p.sku, p.id LIMIT 101
    """, params)
    truncated = len(matches) > 100
    matches = matches[:100]
    if product_id is not None and not any(row['id'] == product_id for row in matches):
        raise HTTPException(400, detail={'code': 'product_trace_selection_invalid', 'message': 'Choose a matching stock card.'})
    selected = product_id if product_id is not None else matches[0]['id'] if len(matches) == 1 and not truncated else None
    warnings = ['sales_hub_ledger_only']
    if not matches:
        warnings.append('product_not_found')
    elif selected is None:
        warnings.append('ambiguous_code')
    params['product_id'] = selected
    barcodes = []
    product = None
    stocks, prices = [], []
    sales = None
    movements = {'items': [], 'total': 0, 'page': movement_page, 'page_size': PAGE_SIZE}
    if selected is not None:
        product = (await rows(db, """SELECT p.id,p.sku,
            COALESCE(o.data #>> '{common,name}',p.name) AS name,
            COALESCE(o.data #>> '{common,brand}',p.brand) AS brand,p.item_type,p.stock_unit
            FROM products p LEFT JOIN product_editor_overrides o ON o.product_id=p.id WHERE p.id=:product_id""", params))[0]
        identifiers = await rows(db, """SELECT value FROM product_identifiers WHERE product_id=:product_id
            AND identifier_type IN ('ean','upc','unverified_barcode') ORDER BY is_primary DESC,value""", params)
        barcodes = list(dict.fromkeys(row['value'] for row in identifiers))
        product.update(eans=barcodes, card_url='/products/' + quote(product['sku'], safe=''))
        stocks = await rows(db, """SELECT w.code AS warehouse_code,w.name AS warehouse_name,
            t.product_id IS NOT NULL AS confirmed,
            CASE WHEN t.product_id IS NOT NULL THEN b.qty_on_hand END AS on_hand,
            CASE WHEN t.product_id IS NOT NULL THEN b.qty_reserved END AS reserved,
            CASE WHEN t.product_id IS NOT NULL THEN b.qty_quarantined END AS quarantined,
            CASE WHEN t.product_id IS NOT NULL THEN b.qty_on_hand-b.qty_reserved-b.qty_quarantined END AS available
            FROM stock_balances b JOIN warehouses w ON w.id=b.warehouse_id
            LEFT JOIN stock_tracking t ON t.product_id=b.product_id AND t.warehouse_id=b.warehouse_id
            WHERE b.product_id=:product_id ORDER BY w.name,w.id""", params)
        if product['item_type'] == 'stock' and (not stocks or any(not row['confirmed'] for row in stocks)):
            warnings.append('stock_not_confirmed')
        prices = await rows(db, """SELECT 'supplier' AS source,s.name AS label,
            COALESCE(ps.negotiated_price,sp.purchase_price) AS price,
            CASE WHEN ps.negotiated_price IS NOT NULL THEN COALESCE(ps.negotiated_currency,sp.purchase_currency)
              ELSE sp.purchase_currency END AS currency,
            NULL::boolean AS vat_included,sp.updated_at AS observed_at
            FROM product_supply_sources ps JOIN supplier_products sp ON sp.id=ps.supplier_product_id
            JOIN suppliers s ON s.id=sp.supplier_id
            WHERE ps.product_id=:product_id AND ps.is_active ORDER BY ps.is_primary DESC,s.name""", params)
        prices += await rows(db, """SELECT 'shop' AS source,s.name AS label,sp.shop_price AS price,
            NULL::text AS currency,NULL::boolean AS vat_included,sp.last_pull_at AS observed_at
            FROM shop_products sp JOIN shops s ON s.id=sp.shop_id
            WHERE sp.product_id=:product_id AND sp.is_listed ORDER BY s.name""", params)
        editor = (await db.execute(text('SELECT data,updated_at FROM product_editor_overrides WHERE product_id=:product_id'), params)).mappings().first()
        if editor:
            data = editor['data'] or {}
            price = data.get('variant', {}).get('sale_price_gross')
            if price is not None:
                prices.append({'source': 'hub_sale', 'label': 'Hub', 'price': str(price), 'currency': 'EUR',
                               'vat_included': True, 'observed_at': serial(editor['updated_at'])})
        sales = (await rows(db, """SELECT COALESCE(SUM(-quantity) FILTER(WHERE movement_type='SALE_OUT'),0) AS sold_quantity,
            COALESCE(SUM(quantity) FILTER(WHERE movement_type='RETURN_IN'),0) AS returned_quantity,
            COALESCE(SUM(-quantity),0) AS net_quantity, MIN(created_at) AS since
            FROM stock_movements WHERE product_id=:product_id AND movement_type IN ('SALE_OUT','RETURN_IN')""", params))[0]
        sales.update(unit=product['stock_unit'], source='hub_ledger')
        sales['monthly'] = await rows(db, """SELECT to_char(date_trunc('month',created_at AT TIME ZONE 'UTC'),'YYYY-MM') AS month,
            COALESCE(SUM(-quantity) FILTER(WHERE movement_type='SALE_OUT'),0) AS sold,
            COALESCE(SUM(quantity) FILTER(WHERE movement_type='RETURN_IN'),0) AS returned,
            COALESCE(SUM(-quantity),0) AS net
            FROM stock_movements WHERE product_id=:product_id AND movement_type IN ('SALE_OUT','RETURN_IN')
            AND created_at >= (date_trunc('month',CURRENT_TIMESTAMP AT TIME ZONE 'UTC')-interval '11 months') AT TIME ZONE 'UTC'
            GROUP BY 1 ORDER BY 1""", params)
        movements = await list_movements(db, sku=product['sku'], page=movement_page, page_size=PAGE_SIZE, tracking_scope='all')
    params.update(barcodes=list(dict.fromkeys(([code] if code else []) + barcodes)), offset=(invoice_page - 1)*PAGE_SIZE, limit=PAGE_SIZE)
    # A proven different product owner wins over a repeated/incorrect barcode.
    where = """((CAST(:product_id AS bigint) IS NOT NULL AND l.matched_product_id=:product_id)
        OR ((CAST(:product_id AS bigint) IS NULL OR l.matched_product_id IS NULL)
            AND (l.barcode_values && CAST(:barcodes AS text[]) OR l.ean=ANY(CAST(:barcodes AS text[])))))"""
    total = await db.scalar(text('SELECT count(*) FROM uploaded_invoice_lines l WHERE '+where), params)
    document_sales = {'source': 'shop_invoice_archive', 'by_unit': [], 'monthly': []}
    if selected is not None:
        document_base = ''' FROM uploaded_invoice_lines l JOIN uploaded_invoices ui ON ui.id=l.invoice_id
            WHERE ui.direction='sale' AND ui.document_type IN ('invoice','credit_note') AND ''' + where
        document_sales['by_unit'] = await rows(db, '''SELECT l.unit,
            COALESCE(SUM(l.quantity) FILTER(WHERE ui.document_type='invoice'),0) AS sold_quantity,
            COALESCE(SUM(ABS(l.quantity)) FILTER(WHERE ui.document_type='credit_note'),0) AS returned_quantity,
            COALESCE(SUM(CASE WHEN ui.document_type='credit_note' THEN -ABS(l.quantity) ELSE l.quantity END),0) AS net_quantity'''
            + document_base + ' GROUP BY l.unit ORDER BY l.unit', params)
        document_sales['monthly'] = await rows(db, '''SELECT to_char(ui.invoice_date,'YYYY-MM') AS month,l.unit,
            COALESCE(SUM(l.quantity) FILTER(WHERE ui.document_type='invoice'),0) AS sold,
            COALESCE(SUM(ABS(l.quantity)) FILTER(WHERE ui.document_type='credit_note'),0) AS returned,
            COALESCE(SUM(CASE WHEN ui.document_type='credit_note' THEN -ABS(l.quantity) ELSE l.quantity END),0) AS net'''
            + document_base + ''' AND ui.invoice_date >= date_trunc('month',CURRENT_DATE)-interval '11 months'
            GROUP BY 1,l.unit ORDER BY 1,l.unit''', params)
    invoice_rows = await rows(db, """SELECT ui.id AS invoice_id,l.id AS line_id,ui.direction,ui.invoice_number,
        ui.invoice_date,ui.due_date,s.name AS supplier_name,sh.code AS shop_code,ui.currency,
        l.product_name,l.quantity,l.unit,l.unit_price,l.total_price,
        CASE WHEN l.matched_product_id=:product_id THEN 'product' ELSE 'barcode' END AS matched_by
        FROM uploaded_invoice_lines l JOIN uploaded_invoices ui ON ui.id=l.invoice_id
        LEFT JOIN suppliers s ON s.id=ui.supplier_id LEFT JOIN shops sh ON sh.id=ui.shop_id
        WHERE """+where+""" ORDER BY ui.invoice_date DESC NULLS LAST,ui.id DESC,l.line_number
        LIMIT :limit OFFSET :offset""", params)
    if await db.scalar(text('SELECT EXISTS(SELECT 1 FROM uploaded_invoices WHERE NOT is_parsed)')):
        warnings.append('invoice_index_incomplete')
    return {'code': code, 'matches': matches, 'matches_truncated': truncated, 'product': product,
            'stocks': stocks, 'prices': prices, 'sales': sales, 'sales_documents': document_sales, 'movements': movements,
            'invoices': {'items': invoice_rows, 'total': int(total or 0), 'page': invoice_page, 'page_size': PAGE_SIZE},
            'warnings': warnings, 'upgates_calls': 0}
