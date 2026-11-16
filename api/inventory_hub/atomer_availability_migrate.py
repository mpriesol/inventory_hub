"""Install local Atomer export state without changing stock or shop settings."""

from pathlib import Path
import psycopg2
from inventory_hub.settings import settings


def main():
    sql = Path(__file__).with_name("migrations") / "055_atomer_availability.sql"
    if not sql.exists():
        sql = (
            Path(__file__).resolve().parents[2]
            / "infra"
            / "db-init"
            / "055_atomer_availability.sql"
        )
    with psycopg2.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        dbname=settings.DB_NAME,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(691432155)")
            cursor.execute(sql.read_text(encoding="utf-8"))
    print("Atomer availability export migration 055 ready")


if __name__ == "__main__":
    main()
