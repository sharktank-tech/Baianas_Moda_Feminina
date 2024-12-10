from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

# Instância do SQLAlchemy
db = SQLAlchemy()

# Instância do LoginManager
login_manager = LoginManager()

def create_app():
    # Criação da instância do Flask
    app = Flask(__name__)

    # Configurações da aplicação
    app.config.from_object('config.Config')  # Certifique-se de ter o arquivo 'config.py' configurado

    # Inicializa as extensões
    db.init_app(app)
    login_manager.init_app(app)

    # Define a rota padrão de login
    login_manager.login_view = 'auth.login'  # Atualize 'auth.login' com sua rota real de login
    login_manager.login_message = 'Por favor, faça login para acessar esta página.'
    login_manager.login_message_category = 'info'

    # Registrar blueprints
    from loja.views.routes import main_blueprint
    from loja.views.admin_routes import admin_blueprint

    app.register_blueprint(main_blueprint)
    app.register_blueprint(admin_blueprint)

    # Define o carregador de usuário para o LoginManager
    from loja.modules.models import Users  # Certifique-se de que o modelo User está no módulo correto

    @login_manager.user_loader
    def load_user(user_id):
        return Users.query.get(int(user_id))

    return app