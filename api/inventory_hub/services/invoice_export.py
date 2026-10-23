"""Bounded accountant bundle of original documents with a currency-aware manifest."""
import csv
import hashlib
import io
from pathlib import Path
from tempfile import SpooledTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import HTTPException
from sqlalchemy import text
from inventory_hub.file_access import stored_document_path
from inventory_hub.settings import settings

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024


def safe_cell(value):
    value = '' if value is None else str(value)
    # Spreadsheet formula injection must not turn supplier descriptions into code.
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else value


def safe_filename(value):
    name = Path(str(value).replace('\\', '/')).name
    name = ''.join(c for c in name if ord(c) >= 32 and ord(c) != 127)
    return name if name not in ('', '.', '..') else 'document'


def make_bundle(documents, root):
    """Prepare completely before returning; a missing file never yields a partial ZIP."""
    spool = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode='w+b')
    manifest = io.StringIO(newline='')
    writer = csv.writer(manifest, delimiter=';')
    writer.writerow(['invoice_id','direction','supplier','shop','invoice_number','invoice_date','due_date',
                     'currency','total_without_vat','vat_amount','total_with_vat','payment_status','paid_amount','outstanding_amount','file','sha256'])
    total_bytes = 0
    try:
        with ZipFile(spool, 'w', compression=ZIP_DEFLATED) as archive:
            for document in documents:
                try:
                    path = stored_document_path(root, document['file_path'])
                    if path.stat().st_size > MAX_FILE_BYTES:
                        raise HTTPException(413, detail={'code': 'invoice_export_too_large'})
                    with path.open('rb') as source:
                        content = source.read(MAX_FILE_BYTES + 1)
                except (OSError, HTTPException) as error:
                    if isinstance(error, HTTPException) and error.status_code == 413:
                        raise
                    raise HTTPException(409, detail={'code': 'invoice_export_file_missing', 'invoice_id': document['id']}) from None
                total_bytes += len(content)
                if len(content) > MAX_FILE_BYTES or total_bytes > MAX_TOTAL_BYTES:
                    raise HTTPException(413, detail={'code': 'invoice_export_too_large'})
                name = f"{document['id']}/{safe_filename(document['original_filename'])}"
                digest = hashlib.sha256(content).hexdigest()
                if document.get('sha256') and digest != document['sha256']:
                    raise HTTPException(409, detail={'code': 'invoice_export_file_changed', 'invoice_id': document['id']})
                archive.writestr(name, content)
                fields = ('id','direction','supplier_name','shop_code','invoice_number','invoice_date','due_date',
                          'currency','total_without_vat','vat_amount','total_with_vat','payment_status','paid_amount','outstanding_amount')
                writer.writerow([safe_cell(document.get(key)) for key in fields] + [safe_cell(name),digest])
            archive.writestr('manifest.csv', '\ufeff' + manifest.getvalue())
        spool.seek(0)
        return spool
    except BaseException:
        spool.close()
        raise


async def export(db, invoice_ids):
    ids = sorted(set(invoice_ids))
    result = await db.execute(text('''SELECT ui.id,ui.direction,ui.original_filename,ui.file_path,
        ui.invoice_number,ui.invoice_date,ui.due_date,ui.currency,ui.total_without_vat,ui.vat_amount,
        ui.total_with_vat,ui.payment_status,ui.paid_amount,s.name AS supplier_name,sh.code AS shop_code
        FROM uploaded_invoices ui LEFT JOIN suppliers s ON s.id=ui.supplier_id
        LEFT JOIN shops sh ON sh.id=ui.shop_id WHERE ui.id=ANY(CAST(:ids AS bigint[])) ORDER BY ui.id'''), {'ids': ids})
    documents = [dict(row) for row in result.mappings().all()]
    if len(documents) != len(ids):
        raise HTTPException(404, detail={'code': 'invoice_export_not_found'})
    from inventory_hub.services import invoice_finance
    finances = {row['id']: invoice_finance.snapshot(row) for row in await invoice_finance.financial_rows(
        db, conditions='ui.id=ANY(CAST(:ids AS bigint[]))', params={'ids': ids})}
    for document in documents:
        finance = finances[document['id']]
        document.update(payment_status=finance['status'], paid_amount=finance['paid_amount'],
                        outstanding_amount=finance['outstanding_amount'], total_with_vat=finance['gross_amount'])
    attachments = (await db.execute(text('''SELECT invoice_id,id,filename,file_path,sha256
        FROM invoice_files WHERE invoice_id=ANY(CAST(:ids AS bigint[])) ORDER BY invoice_id,id'''), {'ids': ids})).mappings().all()
    by_invoice = {}
    for attachment in attachments:
        by_invoice.setdefault(attachment['invoice_id'], []).append(attachment)
    expanded = []
    for document in documents:
        files = by_invoice.get(document['id'], [])
        if not files:
            expanded.append(document)
        else:
            for attachment in files:
                expanded.append({**document, 'file_path': attachment['file_path'], 'sha256': attachment['sha256'],
                    'original_filename': str(attachment['id']) + '-' + attachment['filename']})
    # File I/O and ZIP compression run off the API event loop.
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(make_bundle, expanded, settings.INVENTORY_DATA_ROOT)
