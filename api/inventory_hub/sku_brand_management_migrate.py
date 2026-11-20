"""Install SKU manufacturer archival and historical prefix reservations."""

from pathlib import Path
import psycopg2
from inventory_hub.settings import settings


def main():
    name = "059_sku_brand_management.sql"
    sql = Path(__file__).with_name("migrations") / name
    if not sql.exists():
        sql = Path(__file__).resolve().parents[2] / "infra" / "db-init" / name
    with psycopg2.connect(host=settings.DB_HOST, port=settings.DB_PORT, dbname=settings.DB_NAME,
                          user=settings.DB_USER, password=settings.DB_PASSWORD) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(691432159)")
            cursor.execute(sql.read_text(encoding="utf-8"))
    print("SKU brand management migration 059 ready")


if __name__ == "__main__":
    main()
