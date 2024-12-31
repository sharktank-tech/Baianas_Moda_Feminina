from flask import Blueprint, render_template, flash, redirect, url_for, request
from loja.modules.models import Products, Users, db
from flask_login import login_user, login_required, current_user, logout_user
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash

main_blueprint = Blueprint('main', __name__)

@main_blueprint.route('/')
def home():
    # Obtém todos os produtos
    products = Products.query.limit(4).all()

    # Converte os preços para float de forma segura
    for product in products:
        if product:
            try:
                product.price = float(product.price)
            except (ValueError, TypeError):
                product.price = 0.0  # Define um valor padrão se a conversão falhar
            # Garante uma imagem padrão
            product.image = product.image or url_for('static', filename='img/instagram.png')

    # Verifica se o usuário está autenticado e obtém o nome
    user = current_user.username if current_user.is_authenticated else None
    print(f'Usuário atual: {user}')

    # Renderiza o template com os produtos e o usuário
    return render_template('main/index.html', products=products, user=user)


@main_blueprint.route('/produtos')
def produtos():
    # Exemplo de dados para passar ao template
    ads = [
        {"image": "https://via.placeholder.com/1500x600", "alt": "Anúncio 1"},
        {"image": "https://via.placeholder.com/1500x600", "alt": "Anúncio 2"},
        {"image": "https://via.placeholder.com/1500x600", "alt": "Anúncio 3"}
    ]
    novidades = [
        {"large_image": "https://via.placeholder.com/600x400", "small_images": [
            "https://via.placeholder.com/300x200",
            "https://via.placeholder.com/300x200"
        ]},
        {"large_image": "https://via.placeholder.com/600x400", "small_images": [
            "https://via.placeholder.com/300x200",
            "https://via.placeholder.com/300x200"
        ]}
    ]
    destaques = [
        {"large_image": "https://via.placeholder.com/600x400", "small_images": [
            "https://via.placeholder.com/300x200",
            "https://via.placeholder.com/300x200"
        ]},
        {"large_image": "https://via.placeholder.com/600x400", "small_images": [
            "https://via.placeholder.com/300x200",
            "https://via.placeholder.com/300x200"
        ]}
    ]

    return render_template('main/produtos.html', ads=ads, novidades=novidades, destaques=destaques)


# Rota para a conta
@main_blueprint.route('/conta')
@login_required
def conta():
    try:
        user = current_user
        username = user.username
        id = user.id
        email = user.email
        admin = user.is_admin
        return render_template('conta/conta.html', user=user, admin=admin, id=id, username=username, email=email)
    except Exception as e:
        flash("Erro ao acessar os dados da conta. Tente novamente.", "danger")
        print(f"Erro ao acessar os dados da conta. Tente novamente: {e}", "danger")
        return redirect(url_for('main.home'))

# Rota para a página de cadastro
@main_blueprint.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')  # Ajustado para coincidir com o form
        email = request.form.get('email')
        password = request.form.get('password')

        # Verifica se já existe um usuário com o mesmo nome ou email
        existing_user = Users.query.filter(
            (Users.username == username) | (Users.email == email)
        ).first()

        if existing_user:
            flash("Usuário ou e-mail já existe.", "danger")
            return redirect(url_for('main.register'))

        try:
            # Cria um novo usuário
            new_user = Users(username=username, email=email, is_admin=False)
            new_user.set_password(password)  # Gera o hash da senha
            db.session.add(new_user)
            db.session.commit()

            flash("Cadastro realizado com sucesso! Faça login para continuar.", "success")
            return redirect(url_for('main.login'))
        except IntegrityError:
            db.session.rollback()
            flash("Erro ao salvar usuário. Por favor, tente novamente.", "danger")
        except Exception as e:
            flash(f"Erro inesperado: {str(e)}", "danger")

    return render_template('conta/register.html')


# Rota para a página de login
@main_blueprint.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')  # Corrigir para 'password'

        if not username or not password:  # Verificar se os campos foram preenchidos
            flash("Por favor, preencha todos os campos.", "danger")
            return redirect(url_for('main.login'))

        # Busca o usuário pelo nome de usuário
        user = Users.query.filter_by(username=username).first()

        if not user or not check_password_hash(user.password_hash, password):
            flash("Credenciais inválidas. Tente novamente.", "danger")
            return redirect(url_for('main.login'))

        # Autentica o usuário
        login_user(user)
        flash("Login realizado com sucesso!", "success")
        return redirect(url_for('main.home'))

    return render_template('conta/login.html')

# Rota para logout
@main_blueprint.route('/logout')
@login_required
def logout():
    logout_user()
    flash("Você saiu da sua conta.", "info")
    return redirect(url_for('main.home'))

# Rota para editar conta
@main_blueprint.route('/edit_account', methods=['GET', 'POST'])
@login_required
def edit_account():
    if request.method == 'POST':
        # Obtem os novos dados do formulário
        new_username = request.form.get('username')
        new_email = request.form.get('email')

        # Valida os dados e atualiza o usuário
        if new_username and new_email:
            current_user.username = new_username
            current_user.email = new_email
            db.session.commit()
            flash('Informações atualizadas com sucesso!', 'success')
            return redirect(url_for('main.account'))
        else:
            flash('Por favor, preencha todos os campos.', 'danger')

    # Exibe o formulário com os dados atuais do usuário
    return render_template('conta/edit_account.html', username=current_user.username, email=current_user.email)


# Rota para deletar conta
@main_blueprint.route('/delete_account', methods=['POST'])
@login_required
def delete_account():
    try:
        # Remove o usuário atual do banco de dados
        db.session.delete(current_user)
        db.session.commit()
        flash('Conta excluída com sucesso!', 'success')
        return redirect(url_for('auth.login'))  # Redireciona para a página de login
    except Exception as e:
        flash('Ocorreu um erro ao tentar excluir a conta. Tente novamente.', 'danger')
        return redirect(url_for('main.account'))

# Rota da Politica d eprvacidade
@main_blueprint.route('/politica_privacidade')
def politica_privacidade():
    return render_template('institucional/politica_privacidade.html')

# Rota da página de sobre
@main_blueprint.route('/sobre')
def sobre():
    return render_template('institucional/sobre.html')