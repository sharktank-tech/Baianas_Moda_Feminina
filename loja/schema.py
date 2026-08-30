import os

from sqlalchemy import event, inspect, text


# ---------------------------------------------------------------------------
# Bootstrap de schema aditivo e idempotente (sem Flask-Migrate).
#
# O projeto não usa migrações versionadas. O bootstrap (create_all +
# ensure_schema) ocorre EM AMBIENTE CONTROLADO/local ou quando
# POLLOMODAS_BOOTSTRAP_ON_STARTUP=1. A ordem de bootstrap é:
#
#   1. importar models (legados + P0/P1)
#   2. db.create_all()            -> cria tabelas NOVAS que ainda não existem
#   3. ensure_schema(app)         -> corrige tabelas EXISTENTES (colunas/FKs/índices)
#
# Ambiente serverless (Vercel): create_app() NÃO executa DDL/bootstrap em cold
# start. O banco persistente já foi previamente bootstrapped. Apenas o hook de
# transação SET LOCAL search_path é instalado (configuração do Engine).
#
# Bootstrap controlado (local / POLLOMODAS_BOOTSTRAP_ON_STARTUP=1):
#   db.create_all()  +  ensure_schema(app)
#
# Política de segurança (Fase 0): NUNCA DROP/RENAME/ALTER destrutivo, NUNCA
# SET NOT NULL em coluna legada, NUNCA backfill destrutivo, NUNCA delete.
#
# Isolamento por schema (PolloModas): todas as inspeções e DDL abaixo são
# restritas a DB_SCHEMA. O schema `public` é de outro sistema e é intocável.
# DB_SCHEMA é centralizado e sobrescrevível via env POLLOMODAS_SCHEMA (usado
# pelos testes/smoke para apontar para um schema descartável), mas NUNCA deve
# apontar para public/pollomodas em produção por engano.
# ---------------------------------------------------------------------------

# Schema alvo de toda a aplicação PolloModas. Centralizado em um único ponto.
DB_SCHEMA = os.getenv("POLLOMODAS_SCHEMA", "pollomodas")

# Nomes que jamais podem ser usados como schema descartável de teste/smoke,
# pois pertencem ao Supabase/legado e estão fora do domínio da PolloModas.
RESERVED_DISPOSABLE_SCHEMAS = {"public", "pollomodas", "auth", "storage", "extensions"}

# Schemas que a APLICAÇÃO nunca deve usar como alvo de escrita (create_all /
# ensure_schema). public é legado intocável; auth/storage/extensions são do
# Supabase. pollomodas É um alvo válido (produção), por isso não está aqui.
FORBIDDEN_TARGET_SCHEMAS = {"public", "auth", "storage", "extensions"}


def assert_safe_disposable_schema(name):
    """Recusa nomes reservados ou fora do padrão pollo_smoke_.

    Usado pelo smoke e pelos testes para garantir que nenhum DDL destrutivo
    atinja public/pollomodas ou outros schemas do Supabase.
    """
    if name in RESERVED_DISPOSABLE_SCHEMAS:
        raise AssertionError(
            "schema descartável recusado: %r é reservado (public/pollomodas/auth/"
            "storage/extensions)" % name
        )
    if not isinstance(name, str) or not name.startswith("pollo_smoke_"):
        raise AssertionError(
            "schema descartável recusado: %r deve começar com 'pollo_smoke_'" % name
        )


def install_search_path_hook(engine, schema):
    """Força search_path=<schema> no início de CADA transação.

    O Session Pooler do Supabase em transaction pooling IGNORA
    options=-csearch_path da URL/conexão, então este hook central no engine
    garante o schema alvo independente do pooler. Usa a mesma fonte
    POLLOMODAS_SCHEMA. A qualificação explícita em ensure_schema/inspector é a
    segunda barreira. Não mexer em models.

    Usa set_config(..., true) parametrizado (não interpola o nome do schema em
    SQL) e o 3º argumento `true` torna a mudança local à transação, equivalente
    a SET LOCAL.
    """
    @event.listens_for(engine, "begin")
    def _set_local_search_path(conn):
        conn.execute(
            text("SELECT set_config('search_path', :schema, true)"),
            {"schema": schema},
        )


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
    # supplier_products (frete do fornecedor / upload de rascunho AliExpress)
    ("supplier_products", "freight_status"): {
        "type": "string", "length": 30, "nullable": False, "default": "'unknown'",
    },
    ("supplier_products", "shipping_cost"): {"type": "numeric", "nullable": True},
    ("supplier_products", "landed_cost"): {"type": "numeric", "nullable": True},
    ("supplier_products", "freight_currency"): {
        "type": "string", "length": 3, "nullable": True,
    },
    # product_variants (atributos brutos do SKU AliExpress)
    ("product_variants", "attributes"): {"type": "jsonb", "nullable": True},
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
    # products (precificação dropshipping / Fase 6). Sem margem padrão: a margem
    # alvo precisa ser fornecida; ausência mantém o produto não publicável.
    ("products", "currency"): {
        "type": "string", "length": 3, "nullable": False, "default": "'BRL'",
    },
    ("products", "target_margin_percent"): {"type": "numeric5", "nullable": True},
    ("products", "pricing_status"): {
        "type": "string", "length": 20, "nullable": False, "default": "'unpriced'",
    },
    ("products", "pricing_error"): {"type": "text", "nullable": True},
    ("products", "pricing_updated_at"): {"type": "datetime", "nullable": True},
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
    if generic == "numeric5":
        return "NUMERIC(5,2)"
    if generic == "jsonb":
        return "JSONB" if dialect == "postgresql" else "JSON"
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


def _schema_context(dialect_name):
    """Devolve (schema_arg, qualify) para restringir inspeção/DDL ao DB_SCHEMA.

    Em PostgreSQL, usamos schema=DB_SCHEMA nas inspeções e qualificamos o DDL
    com 'DB_SCHEMA.'. Em SQLite (testes unitários), não há schemas: ignora.
    """
    is_pg = dialect_name == "postgresql"
    schema_arg = DB_SCHEMA if is_pg else None

    def qualify(name):
        return "%s.%s" % (DB_SCHEMA, name) if is_pg else name

    return schema_arg, qualify


def ensure_schema(app):
    """Corrige o schema de um banco já existente (aditivo e idempotente).

    Restrito a DB_SCHEMA. Nunca executa DROP/RENAME/ALTER destrutivo. Silencia
    falhas para não quebrar o boot (serverless/Vercel).
    """
    from loja import db

    try:
        with app.app_context():
            inspector = inspect(db.engine)
            dialect = db.engine.dialect.name
            schema_arg, qualify = _schema_context(dialect)
            existing_tables = set(inspector.get_table_names(schema=schema_arg))

            # 1+2: colunas ausentes em tabelas existentes
            for (table, column), spec in REQUIRED_COLUMNS.items():
                if table not in existing_tables:
                    continue
                cols = {
                    c["name"]
                    for c in inspector.get_columns(table, schema=schema_arg)
                }
                if column in cols:
                    continue
                ddl = _render_column(spec, dialect)
                _run(
                    db,
                    "ALTER TABLE %(tq)s ADD COLUMN %(c)s %(ddl)s"
                    % {"tq": qualify(table), "c": column, "ddl": ddl},
                    app,
                    "Schema: coluna %s.%s criada." % (table, column),
                )

            # 3: FKs retroativas
            for fk in REQUIRED_FOREIGN_KEYS:
                t = fk["table"]
                col = fk["column"]
                if t not in existing_tables or fk["ref_table"] not in existing_tables:
                    continue
                existing = inspector.get_foreign_keys(t, schema=schema_arg)
                already = any(
                    f.get("referred_table") == fk["ref_table"]
                    and col in (f.get("constrained_columns") or [])
                    and fk["ref_column"] in (f.get("referred_columns") or [])
                    for f in existing
                )
                if already:
                    continue
                cols = {
                    c["name"]
                    for c in inspector.get_columns(t, schema=schema_arg)
                }
                coltype = _sql_type(dialect, fk["coltype"])
                if col not in cols:
                    # coluna inexistente: cria já com a FK inline (funciona em
                    # PostgreSQL e SQLite).
                    ddl = (
                        "ALTER TABLE %(tq)s ADD COLUMN %(c)s %(ct)s "
                        "REFERENCES %(rtq)s(%(rc)s) ON DELETE %(od)s"
                        % {
                            "tq": qualify(t), "c": col, "ct": coltype,
                            "rtq": qualify(fk["ref_table"]),
                            "rc": fk["ref_column"], "od": fk["on_delete"],
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
                            "ALTER TABLE %(tq)s ADD CONSTRAINT fk_%(t)s_%(c)s "
                            "FOREIGN KEY (%(c)s) REFERENCES %(rtq)s(%(rc)s) "
                            "ON DELETE %(od)s"
                            % {
                                "t": t,
                                "tq": qualify(t), "c": col,
                                "rtq": qualify(fk["ref_table"]),
                                "rc": fk["ref_column"], "od": fk["on_delete"],
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
                indexes = inspector.get_indexes(table, schema=schema_arg)
                if any(column in (idx.get("column_names") or []) for idx in indexes):
                    continue
                ddl = (
                    "CREATE INDEX IF NOT EXISTS ix_%(t)s_%(c)s "
                    "ON %(tq)s (%(c)s)"
                    % {"t": table, "c": column, "tq": qualify(table)}
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
