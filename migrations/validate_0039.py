"""Validator da migration 0039 — registro manual de compra/despacho.

Verifica tabelas, colunas (tipo + nulabilidade), PKs, FKs, unicidade,
checks e índices. Usado contra schema descartável nos testes PG e contra
`pollomodas` na aplicação real.
"""

from __future__ import annotations

# tabela -> {coluna: (tipo, nullable)}
COLUMNS = {
    "manual_supplier_purchases": {
        "id": ("uuid", False),
        "order_id": ("uuid", False),
        "external_order_id": ("character varying", False),
        "purchased_at": ("timestamp without time zone", False),
        "purchased_tz": ("character varying", False),
        "evidence_ref": ("character varying", False),
        "note": ("text", True),
        "created_by": ("uuid", True),
        "created_at": ("timestamp without time zone", False),
        "updated_at": ("timestamp without time zone", False),
    },
    "manual_supplier_purchase_items": {
        "id": ("uuid", False),
        "purchase_id": ("uuid", False),
        "order_item_id": ("uuid", False),
        "quantity": ("integer", False),
    },
    "shipment_items": {
        "id": ("uuid", False),
        "shipment_id": ("bigint", False),
        "order_item_id": ("uuid", False),
        "quantity": ("integer", False),
    },
    "manual_operational_audit": {
        "id": ("uuid", False),
        "entity": ("character varying", False),
        "entity_id": ("text", False),
        "field": ("character varying", False),
        "old_value": ("text", True),
        "new_value": ("text", True),
        "admin_user_id": ("uuid", True),
        "reason": ("character varying", False),
        "created_at": ("timestamp without time zone", False),
    },
}

# (tabela, referenciada) esperadas como FK.
FOREIGN_KEYS = {
    ("manual_supplier_purchases", "orders"),
    ("manual_supplier_purchases", "users"),
    ("manual_supplier_purchase_items", "manual_supplier_purchases"),
    ("manual_supplier_purchase_items", "order_items"),
    ("shipment_items", "shipments"),
    ("shipment_items", "order_items"),
    ("manual_operational_audit", "users"),
    ("shipments", "manual_supplier_purchases"),
}

# Constraints UNIQUE esperadas por tabela (nomes exatos).
# Unicidade sem contar PKs (contype='p' é verificado à parte acima).
UNIQUES = {
    "manual_supplier_purchases": {
        "manual_supplier_purchases_external_order_id_key",
    },
    "manual_supplier_purchase_items": {"uq_manual_purchase_item"},
    "shipment_items": {"uq_shipment_item"},
    "manual_operational_audit": set(),
}

CHECKS = {
    "ck_manual_purchase_item_qty",
    "ck_shipment_item_qty",
    "ck_manual_audit_entity",
}

INDEXES = {
    "ix_manual_purchases_order",
    "ix_manual_purchase_items_purchase",
    "ix_shipments_manual_purchase",
    "ix_shipment_items_shipment",
    "ix_manual_audit_entity",
}


def validate_schema(conn, schema="pollomodas"):
    errors = []
    cur = conn.cursor()

    for table, expected in COLUMNS.items():
        cur.execute(
            """
            SELECT column_name, data_type,
                   (is_nullable = 'YES') AS nullable
            FROM information_schema.columns
            WHERE table_schema = %(schema)s AND table_name = %(table)s
            """,
            {"schema": schema, "table": table},
        )
        found = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
        if found != expected:
            errors.append(
                f"{table}: esperado {sorted(expected.items())}, "
                f"achado {sorted(found.items())}"
            )

    cur.execute(
        """
        SELECT c.relname, string_agg(a.attname, ',' ORDER BY cols.ord)
        FROM pg_constraint k
        JOIN pg_class c ON c.oid = k.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN LATERAL unnest(k.conkey) WITH ORDINALITY AS cols(attnum, ord)
          ON true
        JOIN pg_attribute a ON a.attrelid = c.oid
          AND a.attnum = cols.attnum
        WHERE n.nspname = %(schema)s AND k.contype = 'p'
        GROUP BY c.relname
        """,
        {"schema": schema},
    )
    pks = {row[0]: row[1] for row in cur.fetchall()}
    for table in COLUMNS:
        if pks.get(table) != "id":
            errors.append(f"{table}: PK ausente ou fora de id: {pks.get(table)}")

    cur.execute(
        """
        SELECT c.relname, f.relname
        FROM pg_constraint k
        JOIN pg_class c ON c.oid = k.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_class f ON f.oid = k.confrelid
        WHERE n.nspname = %(schema)s AND k.contype = 'f'
        """,
        {"schema": schema},
    )
    fks = set(cur.fetchall())
    for table, ref in FOREIGN_KEYS:
        if (table, ref) not in fks:
            errors.append(f"FK ausente: {table} -> {ref} (achadas: {sorted(fks)})")
            break

    cur.execute(
        """
        SELECT c.relname, k.conname
        FROM pg_constraint k
        JOIN pg_class c ON c.oid = k.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %(schema)s AND k.contype = 'u'
        """,
        {"schema": schema},
    )
    uniques = {}
    for table, name in cur.fetchall():
        uniques.setdefault(table, set()).add(name)
    for table, expected in UNIQUES.items():
        if not expected <= uniques.get(table, set()):
            errors.append(
                f"{table}: uniques esperadas {sorted(expected)}, "
                f"achadas {sorted(uniques.get(table, set()))}"
            )

    cur.execute(
        """
        SELECT k.conname FROM pg_constraint k
        JOIN pg_class c ON c.oid = k.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %(schema)s AND k.contype = 'c'
        """,
        {"schema": schema},
    )
    checks = {row[0] for row in cur.fetchall()}
    if not CHECKS <= checks:
        errors.append(f"checks ausentes: {sorted(CHECKS - checks)}")

    cur.execute(
        """
        SELECT indexname FROM pg_indexes WHERE schemaname = %(schema)s
        """,
        {"schema": schema},
    )
    idx = {row[0] for row in cur.fetchall()}
    if not INDEXES <= idx:
        errors.append(f"índices ausentes: {sorted(INDEXES - idx)}")

    cur.execute(
        """
        SELECT to_regclass('public.manual_supplier_purchases'),
               to_regclass('public.shipment_items'),
               to_regclass('public.manual_operational_audit')
        """
    )
    if any(cur.fetchone()):
        errors.append("tabelas manuais existem em public")

    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = %(schema)s AND table_name = 'shipments'
          AND column_name = 'manual_purchase_id'
        """,
        {"schema": schema},
    )
    if not cur.fetchall():
        errors.append("shipments.manual_purchase_id ausente")
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
