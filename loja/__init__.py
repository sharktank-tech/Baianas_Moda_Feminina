from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import text
import uuid

db = SQLAlchemy()
login_manager = LoginManager()
csrf = CSRFProtect()


def create_app():
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

    app.register_blueprint(tracking_blueprint)
    app.register_blueprint(payments_blueprint)
    app.register_blueprint(main_blueprint)
    app.register_blueprint(admin_blueprint)

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

    from loja.schema import (
        ensure_schema,
        DB_SCHEMA,
        install_search_path_hook,
        FORBIDDEN_TARGET_SCHEMAS,
    )

    # Bootstrap do schema dedicado (somente PostgreSQL). Todo o bootstrap e o
    # create_all/ensure_schema ocorrem DENTRO do app context: o acesso a
    # db.engine só acontece após entrar no contexto. Sequência:
    #   app_context -> (PG) validar DB_SCHEMA não proibido
    #   -> instalar hook SET LOCAL search_path (por transação)
    #   -> CREATE SCHEMA IF NOT EXISTS + validar current_schema()
    #   -> db.create_all() -> ensure_schema()
    # O hook de transação é o mecanismo definitivo de isolamento: o Session
    # Pooler do Supabase ignora options=-csearch_path, mas o SET LOCAL roda no
    # início de cada transação e força o schema alvo. Sem fallback para public.
    with app.app_context():
        if db.engine.dialect.name == "postgresql":
            if DB_SCHEMA in FORBIDDEN_TARGET_SCHEMAS:
                raise RuntimeError(
                    "DB_SCHEMA=%r é proibido (não pode ser public/auth/storage/"
                    "extensions). A PolloModas exige o schema dedicado."
                    % DB_SCHEMA
                )
            install_search_path_hook(db.engine, DB_SCHEMA)

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

    return app