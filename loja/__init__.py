from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
import uuid

db = SQLAlchemy()
login_manager = LoginManager()


def create_app():
    app = Flask(__name__)

    app.config.from_object('config.Config')

    db.init_app(app)
    login_manager.init_app(app)

    login_manager.login_view = 'main.login'          # corrigido: main.login
    login_manager.login_message = 'Por favor, faça login para acessar esta página.'
    login_manager.login_message_category = 'info'

    from loja.views.routes import main_blueprint
    from loja.views.admin_routes import admin_blueprint

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

    return app