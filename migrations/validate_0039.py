"""Validator da migration 0039 — registro manual de compra/despacho."""

from __future__ import annotations


def validate_schema(conn, schema="pollomodas"):
    errors = []
    cur = conn.cursor()
    expected_tables = {
        "manual_supplier_purchases": {
            "id", "order_id", "external_order_id", "purchased_at",
            "purchased_tz", "evidence_ref", "note", "created_by",
            "created_at", "updated_at",
        },
        "manual_supplier_purchase_items": {
            "id", "purchase_id", "order_item_id", "quantity",
        },
        "shipment_items": {
            "id", "shipment_id", "order_item_id", "quantity",
        },
        "manual_operational_audit": {
            "id", "entity", "entity_id", "field", "old_value",
            "new_value", "admin_user_id", "reason", "created_at",
        },
    }
    for table, expected in expected_tables.items():
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema=%(schema)s AND table_name=%(table)s
        """, {"schema": schema, "table": table})
        found = {row[0] for row in cur.fetchall()}
        if found != expected:
            errors.append(f"{table}: esperado {sorted(expected)}, achado {sorted(found)}")
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema=%(schema)s AND table_name='shipments'
          AND column_name='manual_purchase_id'
    """, {"schema": schema})
    if not cur.fetchall():
        errors.append("shipments.manual_purchase_id ausente")
    cur.execute("""
        SELECT to_regclass('public.manual_supplier_purchases'),
               to_regclass('public.shipment_items'),
               to_regclass('public.manual_operational_audit')
    """)
    if any(cur.fetchone()):
        errors.append("tabelas manuais existem em public")
    return errors


def main():
    import os
    import psycopg2

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    errors = validate_schema(conn)
    conn.close()
    if errors:
        raise SystemExit("\n".join(errors))
    print("0039 OK")


if __name__ == "__main__":
    main()
