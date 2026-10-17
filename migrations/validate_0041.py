"""Validator da migration 0041 — opt-out da newsletter."""

from __future__ import annotations

COLUMNS = {
    "unsubscribed_at": ("timestamp without time zone", True),
    "resubscribed_at": ("timestamp without time zone", True),
}


def validate_schema(conn, schema="pollomodas"):
    errors = []
    cur = conn.cursor()
    cur.execute(
        """
        SELECT column_name, data_type, (is_nullable = 'YES') AS nullable
        FROM information_schema.columns
        WHERE table_schema = %(schema)s
          AND table_name = 'newsletter_subscribers'
        """,
        {"schema": schema},
    )
    found = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
    for column, (dtype, nullable) in COLUMNS.items():
        if column not in found:
            errors.append(f"newsletter_subscribers.{column} ausente")
            continue
        got_dtype, got_nullable = found[column]
        if got_dtype != dtype:
            errors.append(f"{column}: tipo {got_dtype}, esperado {dtype}")
        if got_nullable != nullable:
            errors.append(f"{column}: nullable={got_nullable}, esperado {nullable}")
    return errors


def main():
    import os
    import psycopg2

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    errors = validate_schema(conn)
    conn.close()
    if errors:
        for error in errors:
            print(f"ERRO: {error}")
        raise SystemExit(1)
    print("OK: newsletter_subscribers.opt-out cols presentes.")


if __name__ == "__main__":
    main()
