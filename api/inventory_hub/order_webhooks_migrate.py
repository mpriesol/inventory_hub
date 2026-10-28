"""Install the opt-in order notification inbox, without activating authority."""
from pathlib import Path
import psycopg2
from inventory_hub.settings import settings


def main():
    sql = Path(__file__).with_name('migrations') / '035_order_webhooks.sql'
    if not sql.exists():
        sql = Path(__file__).resolve().parents[2] / 'infra' / 'db-init' / sql.name
    with psycopg2.connect(host=settings.DB_HOST, port=settings.DB_PORT, dbname=settings.DB_NAME,
                          user=settings.DB_USER, password=settings.DB_PASSWORD) as connection:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(691432135)')
            cursor.execute(sql.read_text(encoding='utf-8'))
    print('Order webhooks migration 035 ready')


if __name__ == '__main__':
    main()
