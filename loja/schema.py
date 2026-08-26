from sqlalchemy import inspect, text


# Colunas adicionadas ao modelo após o banco já estar em produção.
# O projeto não usa Flask-Migrate, então o schema é corrigido sob demanda
# no primeiro boot (ambientes serverless como Vercel).
#
# Política: só colunas ADITIVAS e idempotentes (nullable ou com DEFAULT).
# Tabelas novas são criadas por db.create_all() no boot; este dicionário
# cobre apenas colunas de tabelas que já existiam.
REQUIRED_COLUMNS = {
    "orders": {
        "stock_reserved": "BOOLEAN NOT NULL DEFAULT FALSE",
    },
    "suppliers": {
        "channel_type": "VARCHAR(20) NOT NULL DEFAULT 'manual'",
        "api_key": "TEXT",
        "api_secret": "TEXT",
        "access_token": "TEXT",
        "access_token_expires_at": "TIMESTAMP",
        "seller_id": "VARCHAR(64)",
        "default_currency": "VARCHAR(3) NOT NULL DEFAULT 'BRL'",
        "is_sync_enabled": "BOOLEAN NOT NULL DEFAULT FALSE",
        "last_sync_at": "TIMESTAMP",
        "sync_status": "VARCHAR(20)",
        "sync_error": "TEXT",
    },
    "products": {
        "external_id": "VARCHAR(64)",
        "source": "VARCHAR(20) NOT NULL DEFAULT 'internal'",
        "is_draft": "BOOLEAN NOT NULL DEFAULT FALSE",
        "cost": "NUMERIC(10,2)",
        "wholesale_price": "NUMERIC(10,2)",
        "category_id": "UUID REFERENCES categories(id) ON DELETE SET NULL",
        "sync_status": "VARCHAR(20)",
        "last_sync_at": "TIMESTAMP",
        "sync_error": "TEXT",
    },
    "shipments": {
        "ali_order_id": "VARCHAR(64)",
        "external_shipment_id": "VARCHAR(64)",
    },
    "cart_items": {
        "variant_id": "UUID REFERENCES product_variants(id) ON DELETE SET NULL",
    },
    "order_items": {
        "variant_id": "UUID REFERENCES product_variants(id) ON DELETE SET NULL",
    },
}


def ensure_schema(app):
    """Garante que colunas novas existam no banco, criando-as se faltarem.

    Seguro de rodar a cada inicialização: é idempotente (inspeciona antes de
    alterar) e silencia falhas para não quebrar a aplicação.
    """
    from loja import db

    try:
        with app.app_context():
            inspector = inspect(db.engine)
            for table, columns in REQUIRED_COLUMNS.items():
                if not inspector.has_table(table):
                    continue

                existing = {col["name"] for col in inspector.get_columns(table)}
                for column, ddl in columns.items():
                    if column in existing:
                        continue

                    with db.engine.connect() as conn:
                        conn.execute(
                            text(
                                f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                            )
                        )
                        conn.commit()

                    app.logger.info(
                        "Coluna %s.%s criada com sucesso.", table, column
                    )

    except Exception:
        app.logger.exception(
            "Não foi possível garantir o schema do banco (colunas ausentes)."
        )
