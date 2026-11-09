"""Read exact private source bytes pinned to an existing reconciliation run."""
import asyncio
import zlib

from sqlalchemy import text

from inventory_hub.services import atomer_new_source as source
from inventory_hub.services.atomer_review_targets import ReviewError


FORMATS = {'buyers': ('buyers', 'xml', 'application/xml'),
           'admin': ('admin_csv', 'csv', 'text/csv')}


async def download(db, run_id, kind):
    if kind not in FORMATS:
        raise ReviewError('atomer_source_export_kind_invalid', 422)
    fmt, extension, media_type = FORMATS[kind]
    # Limit even the encoded value returned by PostgreSQL. The allowance covers
    # zlib framing of an incompressible source at the existing upload size limit.
    row = (await db.execute(text("""
        SELECT r.id, e.sha256, e.imported_at, octet_length(e.compressed) AS encoded_size,
               CASE WHEN octet_length(e.compressed)<=:maximum THEN e.compressed END AS compressed
        FROM atomer_progress_runs r
        LEFT JOIN atomer_review_exports e ON e.sha256 =
          CASE WHEN :format='buyers' THEN r.source_sha256 ELSE r.admin_export_sha256 END
          AND e.format=:format
        WHERE r.id=:run
    """), {'run': run_id, 'format': fmt, 'maximum': source.MAX_ARCHIVE + 65_536})).mappings().first()
    if row is None or row['sha256'] is None:
        raise ReviewError('atomer_source_export_missing', 404)
    if row['compressed'] is None:
        raise ReviewError('atomer_source_export_size', 409)
    try:
        # Reuse the migration's bounded decompressor and exact SHA-256 check.
        raw = await asyncio.to_thread(source.hash_source, bytes(row['compressed']), row['sha256'])
        if not raw:
            raise ReviewError('atomer_source_export_invalid')
    except (ReviewError, zlib.error, TypeError, ValueError):
        raise ReviewError('atomer_source_export_invalid', 409) from None
    return {'body': raw, 'sha256': row['sha256'], 'run_id': row['id'],
            'filename': f'atomer-{kind}-run-{row["id"]}.{extension}', 'media_type': media_type}
