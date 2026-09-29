"""Apply the real additive migration twice in an isolated local PG schema."""
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from conftest import ORIGINAL_DATABASE_URL

DATABASE_URL = ORIGINAL_DATABASE_URL
try:
    url = make_url(DATABASE_URL)
    SAFE = url.get_backend_name() == 'postgresql' and url.host in ('localhost', '127.0.0.1', '::1') and url.database.startswith('pollo')
except Exception:
    SAFE = False
pytestmark = pytest.mark.skipif(not SAFE, reason='requires disposable local PostgreSQL')


def test_tax_migration_preserves_unknowns_and_rejects_invalid_evidence():
    engine = create_engine(DATABASE_URL)
    schema = 'pollo_test_tax_' + uuid.uuid4().hex[:10]
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            conn.exec_driver_sql(f'CREATE TABLE "{schema}".supplier_products (id uuid PRIMARY KEY)')
            conn.exec_driver_sql(f'''CREATE TABLE "{schema}".order_items (
                id integer PRIMARY KEY, supplier_unit_cost_snapshot numeric(10,2),
                supplier_shipping_cost_snapshot numeric(10,2), supplier_landed_cost_snapshot numeric(10,2))''')
            for filename in ('0025_supplier_reference_freight_quotes.sql', '0026_freight_evidence_scope.sql'):
                conn.exec_driver_sql(Path('migrations', filename).read_text().replace('pollomodas.', f'"{schema}".'))
            conn.exec_driver_sql(f'INSERT INTO "{schema}".order_items VALUES (1, 10, 0, 10)')
            migration = Path('migrations/20260928161025_supplier_acquisition_taxes.sql').read_text().replace('pollomodas.', f'"{schema}".')
            conn.exec_driver_sql(migration)
            conn.exec_driver_sql(migration)
            row = conn.exec_driver_sql(f'SELECT supplier_import_duty_snapshot, supplier_icms_snapshot FROM "{schema}".order_items').one()
            assert tuple(row) == (None, None)
        for duty, icms in [('-1', '0'), ('NaN', '0'), (None, '0')]:
            with pytest.raises(DBAPIError):
                with engine.begin() as conn:
                    conn.execute(text(f'UPDATE "{schema}".order_items SET supplier_import_duty_snapshot=:duty, supplier_icms_snapshot=:icms'), {'duty': duty, 'icms': icms})
        with engine.begin() as conn:
            conn.exec_driver_sql(f'UPDATE "{schema}".order_items SET supplier_import_duty_snapshot=0, supplier_icms_snapshot=0')
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        engine.dispose()
