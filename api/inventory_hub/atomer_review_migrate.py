"""Install persistent Atomer review decisions and candidate checks."""
from pathlib import Path
import psycopg2
from inventory_hub.settings import settings


def main():
    sql = Path(__file__).with_name('migrations') / '038_atomer_review.sql'
    if not sql.exists():
        sql = Path(__file__).resolve().parents[2] / 'infra' / 'db-init' / sql.name
    with psycopg2.connect(host=settings.DB_HOST, port=settings.DB_PORT, dbname=settings.DB_NAME,
                         user=settings.DB_USER, password=settings.DB_PASSWORD) as connection:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(691432138)')
            cursor.execute(sql.read_text(encoding='utf-8'))
            cursor.execute(sql.with_name('039_atomer_variant_repairs.sql').read_text(encoding='utf-8'))
            cursor.execute(sql.with_name('040_atomer_catalog_campaign.sql').read_text(encoding='utf-8'))
            cursor.execute(sql.with_name('041_atomer_catalog_content.sql').read_text(encoding='utf-8'))
            cursor.execute(sql.with_name('042_atomer_category_restore.sql').read_text(encoding='utf-8'))
            cursor.execute(sql.with_name('043_atomer_source_cards.sql').read_text(encoding='utf-8'))
            cursor.execute(sql.with_name('044_atomer_progress_target.sql').read_text(encoding='utf-8'))
            cursor.execute(sql.with_name('045_atomer_catalog_usage.sql').read_text(encoding='utf-8'))
    print('Atomer review migrations 038–045 ready')


if __name__ == '__main__':
    main()
