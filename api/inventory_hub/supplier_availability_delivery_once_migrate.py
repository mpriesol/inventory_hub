"""Install unchanged migration 034 once, preserving later schedule choices.

034 creates its delivery table and changes schedules in one transaction. The
committed table is its completion checkpoint; replaying its one-time daily
policy on every deployment would reset explicitly configured Husky intervals.
"""
from pathlib import Path

import psycopg2

from inventory_hub.settings import settings


def apply_once(cursor, sql):
    cursor.execute("SELECT pg_advisory_xact_lock(691432134)")
    cursor.execute("SELECT to_regclass('supplier_availability_deliveries') IS NOT NULL")
    if cursor.fetchone()[0]:
        return False
    cursor.execute(sql)
    return True


def main():
    sql = Path(__file__).with_name("migrations") / "034_supplier_availability_delivery.sql"
    if not sql.exists():
        sql = Path(__file__).resolve().parents[2] / "infra" / "db-init" / sql.name
    with psycopg2.connect(host=settings.DB_HOST, port=settings.DB_PORT, dbname=settings.DB_NAME,
                          user=settings.DB_USER, password=settings.DB_PASSWORD) as connection:
        with connection.cursor() as cursor:
            installed = apply_once(cursor, sql.read_text(encoding="utf-8"))
    print("Supplier availability delivery migration 034 " + ("installed" if installed else "already installed"))


if __name__ == "__main__":
    main()
