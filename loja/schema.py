from sqlalchemy import inspect, text


# ---------------------------------------------------------------------------
# Bootstrap de schema aditivo e idempotente (sem Flask-Migrate).
#
# O projeto não usa migrações versionadas: o schema é corrigido sob demanda
# no primeiro boot (ambientes serverless como Vercel). A ordem de boot é:
#
#   1. importar models (legados + P0/P1)
#   2. db.create_all()            -> cria tabelas NOVAS que ainda não existem
#   3. ensure_schema(app)         -> corrige tabelas EXISTENTES (colunas/FKs/índices)
#
# Política de segurança (Fase 0): NUNCA DROP/RENAME/ALTER destrutivo, NUNCA
# SET NOT NULL em coluna legada, NUNCA backfill destrutivo, NUNCA delete.
# ---------------------------------------------------------------------------


# Colunas aditivas em tabelas JÁ EXISTENTES em produção.
# Valor = tipo SQL genérico + metadados; o DDL real é montado por dialecto
# em _render_column(). Apenas colunas que podem estar AUSENTES num banco
# antigo entram aqui. (category_id / variant_id são tratados em REQUIRED_FOREIGN_KEYS.)
REQUIRED_COLUMNS = {
    ("orders", "stock_reserved"): {
        "type": "boolean", "nullable": False, "default": "FALSE",
    },
    # suppliers (dropshipping)
    ("suppliers", "channel_type"): {
        "type": "string", "length": 20, "nullable": False, "default": "'manual'",
    },
    ("suppliers", "api_key"): {"type": "text", "nullable": True},
    ("suppliers", "api_secret"): {"type": "text", "nullable": True},
    ("suppliers", "access_token"): {"type": "text", "nullable": True},
    ("suppliers", "access_token_expires_at"): {"type": "datetime", "nullable": True},
    ("suppliers", "seller_id"): {"type": "string", "length": 64, "nullable": True},
    ("suppliers", "default_currency"): {
        "type": "string", "length": 3, "nullable": False, "default": "'BRL'",
    },
    ("suppliers", "is_sync_enabled"): {
        "type": "boolean", "nullable": False, "default": "FALSE",
    },
    ("suppliers", "last_sync_at"): {"type": "datetime", "nullable": True},
    ("suppliers", "sync_status"): {"type": "string", "length": 20, "nullable": True},
    ("suppliers", "sync_error"): {"type": "text", "nullable": True},
    # products (dropshipping / sincronização)
    ("products", "external_id"): {"type": "string", "length": 64, "nullable": True},
    ("products", "source"): {
        "type": "string", "length": 20, "nullable": False, "default": "'internal'",
    },
    ("products", "is_draft"): {
        "type": "boolean", "nullable": False, "default": "FALSE",
    },
    ("products", "cost"): {"type": "numeric", "nullable": True},
    ("products", "wholesale_price"): {"type": "numeric", "nullable": True},
    ("products", "sync_status"): {"type": "string", "length": 20, "nullable": True},
    ("products", "last_sync_at"): {"type": "datetime", "nullable": True},
    ("products", "sync_error"): {"type": "text", "nullable": True},
    # shipments (rastreio de fornecedor)
    ("shipments", "ali_order_id"): {"type": "string", "length": 64, "nullable": True},
    ("shipments", "external_shipment_id"): {
        "type": "string", "length": 64, "nullable": True,
    },
}

# FKs retroativas em tabelas existentes. db.create_all() NÃO cria FK em tabela
# já existente, então garantimos aqui, idempotentemente, comparando tabela/coluna
# local com tabela/coluna referenciada (não apenas o nome da constraint).
REQUIRED_FOREIGN_KEYS = [
    {
        "table": "products", "column": "category_id", "coltype": "uuid",
        "ref_table": "categories", "ref_column": "id", "on_delete": "SET NULL",
    },
    {
        "table": "cart_items", "column": "variant_id", "coltype": "uuid",
        "ref_table": "product_variants", "ref_column": "id", "on_delete": "SET NULL",
    },
    {
        "table": "order_items", "column": "variant_id", "coltype": "uuid",
        "ref_table": "product_variants", "ref_column": "id", "on_delete": "SET NULL",
    },
]

# Índices retroativos (idempotentes por coluna, independente do nome).
REQUIRED_INDEXES = [
    ("products", "category_id"),
    ("cart_items", "variant_id"),
    ("order_items", "variant_id"),
    ("shipments", "ali_order_id"),
    ("shipments", "external_shipment_id"),
]


def _sql_type(dialect, generic):
    if generic == "uuid":
        return "UUID" if dialect == "postgresql" else "VARCHAR(36)"
    if generic == "string":
        return "VARCHAR(%(length)s)"
    if generic == "text":
        return "TEXT"
    if generic == "numeric":
        return "NUMERIC(10,2)"
    if generic == "datetime":
        return "TIMESTAMP" if dialect == "postgresql" else "DATETIME"
    if generic == "boolean":
        return "BOOLEAN"
    raise ValueError("tipo SQL genérico desconhecido: %s" % generic)


def _render_column(spec, dialect):
    sql = _sql_type(dialect, spec["type"])
    if spec["type"] == "string":
        sql = "VARCHAR(%d)" % int(spec["length"])
    parts = [sql]
    if not spec.get("nullable", True):
        parts.append("NOT NULL")
    default = spec.get("default")
    if default is not None:
        parts.append("DEFAULT %s" % default)
    return " ".join(parts)


def _run(db, ddl, app, ok_message):
    with db.engine.connect() as conn:
        conn.execute(text(ddl))
        conn.commit()
    app.logger.info(ok_message)


def ensure_schema(app):
    """Corrige o schema de um banco já existente (aditivo e idempotente).

    Responsabilidade clara:
      1. detectar tabela
      2. adicionar coluna ausente
      3. garantir FK ausente
      4. garantir índice ausente

    Nunca executa DROP/RENAME/ALTER destrutivo. Silencia falhas para não
    quebrar o boot (serverless/Vercel).
    """
    from loja import db

    try:
        with app.app_context():
            inspector = inspect(db.engine)
            dialect = db.engine.dialect.name
            existing_tables = set(inspector.get_table_names())

            # 1+2: colunas ausentes em tabelas existentes
            for (table, column), spec in REQUIRED_COLUMNS.items():
                if table not in existing_tables:
                    continue
                cols = {c["name"] for c in inspector.get_columns(table)}
                if column in cols:
                    continue
                ddl = _render_column(spec, dialect)
                _run(
                    db,
                    "ALTER TABLE %(t)s ADD COLUMN %(c)s %(ddl)s"
                    % {"t": table, "c": column, "ddl": ddl},
                    app,
                    "Schema: coluna %s.%s criada." % (table, column),
                )

            # 3: FKs retroativas
            for fk in REQUIRED_FOREIGN_KEYS:
                t = fk["table"]
                col = fk["column"]
                if t not in existing_tables or fk["ref_table"] not in existing_tables:
                    continue
                existing = inspector.get_foreign_keys(t)
                already = any(
                    f.get("referred_table") == fk["ref_table"]
                    and col in (f.get("constrained_columns") or [])
                    and fk["ref_column"] in (f.get("referred_columns") or [])
                    for f in existing
                )
                if already:
                    continue
                cols = {c["name"] for c in inspector.get_columns(t)}
                coltype = _sql_type(dialect, fk["coltype"])
                if col not in cols:
                    # coluna inexistente: cria já com a FK inline (funciona em
                    # PostgreSQL e SQLite).
                    ddl = (
                        "ALTER TABLE %(t)s ADD COLUMN %(c)s %(ct)s "
                        "REFERENCES %(rt)s(%(rc)s) ON DELETE %(od)s"
                        % {
                            "t": t, "c": col, "ct": coltype,
                            "rt": fk["ref_table"], "rc": fk["ref_column"],
                            "od": fk["on_delete"],
                        }
                    )
                    _run(
                        db, ddl, app,
                        "Schema: coluna+FK %s.%s criadas." % (t, col),
                    )
                else:
                    # coluna existe, falta só a FK. PostgreSQL suporta
                    # ADD CONSTRAINT; SQLite não permite em coluna existente.
                    if dialect == "postgresql":
                        ddl = (
                            "ALTER TABLE %(t)s ADD CONSTRAINT fk_%(t)s_%(c)s "
                            "FOREIGN KEY (%(c)s) REFERENCES %(rt)s(%(rc)s) "
                            "ON DELETE %(od)s"
                            % {
                                "t": t, "c": col,
                                "rt": fk["ref_table"], "rc": fk["ref_column"],
                                "od": fk["on_delete"],
                            }
                        )
                        _run(
                            db, ddl, app,
                            "Schema: FK %s.%s garantida." % (t, col),
                        )
                    else:
                        app.logger.warning(
                            "Schema: FK %s.%s ausente e nao pode ser adicionada "
                            "em SQLite (coluna ja existe). Recrie a tabela ou "
                            "use PostgreSQL.", t, col,
                        )

            # 4: índices retroativos
            for (table, column) in REQUIRED_INDEXES:
                if table not in existing_tables:
                    continue
                indexes = inspector.get_indexes(table)
                if any(column in (idx.get("column_names") or []) for idx in indexes):
                    continue
                ddl = (
                    "CREATE INDEX IF NOT EXISTS ix_%(t)s_%(c)s "
                    "ON %(t)s (%(c)s)" % {"t": table, "c": column}
                )
                _run(
                    db, ddl, app,
                    "Schema: indice %s.%s criado." % (table, column),
                )

    except Exception:
        app.logger.exception(
            "Nao foi possivel garantir o schema do banco "
            "(colunas/FKs/indices ausentes)."
        )
