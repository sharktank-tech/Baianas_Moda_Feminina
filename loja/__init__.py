import os

import logging

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import text
import uuid

# Em ambientes serverless (Vercel) o logger de acesso do Werkzeug registraria a
# query string completa do callback OAuth (code/state) em cada request. Em vez
# de reduzir o nível, desligamos o access log do Werkzeug ANTES de servir
# requests. Logs próprios da aplicação (OAuth/API, já sanitizados) permanecem.
def _harden_werkzeug_access_log() -> None:
    if os.environ.get("VERCEL"):
        logging.getLogger("werkzeug").disabled = True

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()


def _should_bootstrap_database_on_startup():
    """Decide se o bootstrap de banco (DDL/create_all) roda em create_app().

    Regras:
      - POLLOMODAS_BOOTSTRAP_ON_STARTUP definido: obedece explicitamente
        (1/true/yes/on => sim; qualquer outro => não).
      - Ausente: em ambiente Vercel (VERCEL definido) NÃO roda (cold start
        serverless não deve abrir conexão nem executar migração); em ambiente
        local/controlado roda.
    """
    explicit = os.environ.get("POLLOMODAS_BOOTSTRAP_ON_STARTUP")
    if explicit is not None:
        return explicit.strip().lower() in {"1", "true", "yes", "on"}
    # Vercel Functions não executam bootstrap/migração em cold start.
    return not bool(os.environ.get("VERCEL"))


def is_postgresql_database():
    """True se o banco configurado for PostgreSQL (sem abrir conexão)."""
    return db.engine.dialect.name == "postgresql"


def _bootstrap_database(app):
    """Bootstrap controlado: cria schema, valida search_path, cria tabelas e
    corrige o schema existente. SOMENTE em ambiente não-serverless ou com
    POLLOMODAS_BOOTSTRAP_ON_STARTUP=1.

    Não é chamado em cold start serverless (Vercel).
    """
    from loja.schema import ensure_schema, DB_SCHEMA

    with db.engine.connect() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS %s" % DB_SCHEMA))
        conn.commit()
    with db.engine.connect() as conn:
        effective = conn.execute(text("SELECT current_schema()")).scalar()
        configured = conn.execute(text("SHOW search_path")).scalar()
    if effective != DB_SCHEMA:
        raise RuntimeError(
            "search_path inválido: current_schema()=%r, "
            "search_path configurado=%r, esperado DB_SCHEMA=%r. "
            "O hook de transação (SET LOCAL search_path) não está sendo "
            "aplicado." % (effective, configured, DB_SCHEMA)
        )

    # create_all(): cria apenas tabelas NOVAS dentro do schema alvo.
    db.create_all()

    # Corrige tabelas EXISTENTES de forma aditiva/idempotente.
    ensure_schema(app)


def _setup_database(app):
    """Configura o acesso ao banco em create_app().

    PostgreSQL:
      - validação de DB_SCHEMA proibido (obrigatória)
      - install_search_path_hook: SEMPRE (configuração do Engine, sem DDL)
      - bootstrap (DDL/create_all/ensure_schema): SOMENTE se
        _should_bootstrap_database_on_startup() for True
    SQLite (testes/locais):
      - cria tabelas e corrige schema (comportamento da suíte atual)
    """
    from loja.schema import (
        ensure_schema,
        DB_SCHEMA,
        install_search_path_hook,
        FORBIDDEN_TARGET_SCHEMAS,
    )

    with app.app_context():
        # SQLite: não há schema dedicado; cria tabelas e corrige direto.
        if not is_postgresql_database():
            db.create_all()
            ensure_schema(app)
            return

        # PostgreSQL: validação de segurança do schema alvo.
        if DB_SCHEMA in FORBIDDEN_TARGET_SCHEMAS:
            raise RuntimeError(
                "DB_SCHEMA=%r é proibido (não pode ser public/auth/storage/"
                "extensions). A PolloModas exige o schema dedicado."
                % DB_SCHEMA
            )

        # SEMPRE em instâncias PostgreSQL: instala o hook SET LOCAL
        # search_path (Engine config apenas; não abre conexão de bootstrap).
        install_search_path_hook(db.engine, DB_SCHEMA)

        # Somente bootstrap explícito / ambiente não-serverless. Em cold start
        # serverless (Vercel) NÃO abrimos conexão nem executamos DDL.
        if _should_bootstrap_database_on_startup():
            _bootstrap_database(app)


def create_app():
    # Antes de começar a servir requests: em Vercel, desliga o access log do
    # Werkzeug (nunca registra ?code=/&state= do callback OAuth em log).
    _harden_werkzeug_access_log()

    app = Flask(__name__)

    app.config.from_object('config.Config')

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    login_manager.login_view = 'main.login'          # corrigido: main.login
    login_manager.login_message = 'Por favor, faça login para acessar esta página.'
    login_manager.login_message_category = 'info'

    from loja.views.routes import main_blueprint
    from loja.views.admin_routes import admin_blueprint
    from loja.views.routes import tracking_blueprint
    from loja.views.payments_routes import payments_blueprint
    from loja.integrations.aliexpress.routes import aliexpress_blueprint

    app.register_blueprint(tracking_blueprint)
    app.register_blueprint(payments_blueprint)
    app.register_blueprint(main_blueprint)
    app.register_blueprint(admin_blueprint)
    app.register_blueprint(aliexpress_blueprint)

    from loja.modules.models import User             # corrigido: User

    @login_manager.user_loader
    def load_user(user_id):
        try:
            return db.session.get(User, uuid.UUID(
                user_id))  # Implementado uso da lib uuid para corresponder ao armazenamento no banco de dados
        except ValueError:
            return None         # corrigido: User

    from loja import cli
    app.cli.add_command(cli.create_admin)

    # Bootstrap do schema dedicado (somente PostgreSQL) + hook de search_path.
    # Em Vercel (serverless) o bootstrap é pulado; o hook continua instalado.
    _setup_database(app)

    return app
