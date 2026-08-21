from sqlalchemy import inspect, text


# Colunas adicionadas ao modelo após o banco já estar em produção.
# O projeto não usa Flask-Migrate, então o schema é corrigido sob demanda
# no primeiro boot (ambientes serverless como Vercel).
REQUIRED_ORDER_COLUMNS = {
    "stock_reserved": "BOOLEAN NOT NULL DEFAULT FALSE",
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
            if not inspector.has_table("orders"):
                return

            existing = {col["name"] for col in inspector.get_columns("orders")}
            for column, ddl in REQUIRED_ORDER_COLUMNS.items():
                if column in existing:
                    continue

                with db.engine.connect() as conn:
                    conn.execute(
                        text(f"ALTER TABLE orders ADD COLUMN {column} {ddl}")
                    )
                    conn.commit()

                app.logger.info("Coluna orders.%s criada com sucesso.", column)

    except Exception:
        app.logger.exception(
            "Não foi possível garantir o schema do banco (colunas ausentes)."
        )