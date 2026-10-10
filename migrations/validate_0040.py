"""Validator da migration 0040 — slides responsivos."""

from __future__ import annotations

COLUMNS = {
    "image_mobile": ("text", True),
    "focal_desktop": ("character varying", False),
    "focal_mobile": ("character varying", False),
    "content_position": ("character varying", False),
    "description": ("text", True),
    "show_title": ("boolean", False),
    "show_description": ("boolean", False),
}


def validate_schema(conn, schema="pollomodas"):
    errors = []
    cur = conn.cursor()
    cur.execute(
        """
        SELECT column_name, data_type, (is_nullable = 'YES') AS nullable,
               column_default
        FROM information_schema.columns
        WHERE table_schema = %(schema)s AND table_name = 'hero_slides'
        """,
        {"schema": schema},
    )
    found = {row[0]: (row[1], row[2], row[3]) for row in cur.fetchall()}
    for column, (dtype, nullable) in COLUMNS.items():
        if column not in found:
            errors.append(f"hero_slides.{column} ausente")
            continue
        got_dtype, got_nullable, default = found[column]
        if got_dtype != dtype:
            errors.append(f"{column}: tipo {got_dtype}, esperado {dtype}")
        if got_nullable != nullable:
            errors.append(f"{column}: nullable={got_nullable}")
    defaults = {
        "focal_desktop": "center", "focal_mobile": "center",
        "content_position": "left",
    }
    for column, expected in defaults.items():
        if column in found and (found[column][2] or "") != f"'{expected}'::character varying":
            errors.append(f"{column}: default inesperado: {found[column][2]}")
    for column in ("show_title", "show_description"):
        if column in found and (found[column][2] or "") != "true":
            errors.append(f"{column}: default inesperado: {found[column][2]}")
    cur.execute(
        """
        SELECT to_regclass('public.hero_slides')
        """
    )
    if cur.fetchone()[0] is not None:
        errors.append("hero_slides existe em public")
    return errors


def main():
    import os
    import psycopg2

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    errors = validate_schema(conn)
    conn.close()
    if errors:
        raise SystemExit("\n".join(errors))
    print("0040 OK")


if __name__ == "__main__":
    main()
