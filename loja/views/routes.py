from flask import Blueprint, render_template, flash, redirect, url_for, request
from flask_login import login_user, login_required, current_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy.exc import IntegrityError

from loja.modules.models import (
    db,
    Product,
    User,
    HeroSlide,
    Promotion,
    Testimonial,
    SiteConfig,
    FooterConfig,
    SocialLink,
    NavLink,
    CategoryLink,
    ContactInfo,
    LegalLink
)
from loja.modules.services import get_home_data

main_blueprint = Blueprint('main', __name__)


# ── Home ──────────────────────────────────────────────────────────────────────

@main_blueprint.route('/')
def home():
    context = get_home_data()
    return render_template("main/index.html", **context)


# ── Produtos ──────────────────────────────────────────────────────────────────

@main_blueprint.route('/produtos')
def produtos():
    ads = [
        {"image": "https://via.placeholder.com/1500x600", "alt": "Anúncio 1"},
        {"image": "https://via.placeholder.com/1500x600", "alt": "Anúncio 2"},
        {"image": "https://via.placeholder.com/1500x600", "alt": "Anúncio 3"},
    ]
    novidades = [
        {
            "large_image": "https://via.placeholder.com/600x400",
            "small_images": [
                "https://via.placeholder.com/300x200",
                "https://via.placeholder.com/300x200",
            ],
        },
        {
            "large_image": "https://via.placeholder.com/600x400",
            "small_images": [
                "https://via.placeholder.com/300x200",
                "https://via.placeholder.com/300x200",
            ],
        },
    ]
    destaques = [
        {
            "large_image": "https://via.placeholder.com/600x400",
            "small_images": [
                "https://via.placeholder.com/300x200",
                "https://via.placeholder.com/300x200",
            ],
        },
        {
            "large_image": "https://via.placeholder.com/600x400",
            "small_images": [
                "https://via.placeholder.com/300x200",
                "https://via.placeholder.com/300x200",
            ],
        },
    ]
    return render_template(
        'main/produtos.html',
        ads=ads,
        novidades=novidades,
        destaques=destaques,
    )


# ── Conta ─────────────────────────────────────────────────────────────────────

@main_blueprint.route('/conta')
@login_required
def conta():
    try:
        user = current_user
        return render_template(
            'conta/conta.html',
            user=user,
            admin=user.is_admin,
            id=user.id,
            username=user.name,       # corrigido: user.name
            email=user.email,
        )
    except Exception as e:
        flash("Erro ao acessar os dados da conta. Tente novamente.", "danger")
        print(f"Erro ao acessar os dados da conta: {e}")
        return redirect(url_for('main.home'))


# ── Cadastro ──────────────────────────────────────────────────────────────────
@main_blueprint.route("/register",methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = (request.form.get("username", "").strip())
        email = (request.form.get("email", "").strip().lower())
        telefone = (request.form.get("telefone", "").strip()
        password = request.form.get("password","")
        print(username, email, telefone, password)

        # Validação
        if not username or not email or not password:
            flash("Todos os campos são obrigatórios.","danger")
            return redirect(url_for("main.register"))
        try:
            # Verifica usuário existente
            existing_user = User.query.filter((User.email == email) | (User.name == username)).first()

            if existing_user:
                flash("Usuário ou e-mail já cadastrado.","danger")
                return redirect(url_for("main.register"))

            # Cria usuário
            new_user = User(name=username,email=email,is_admin=False)
            new_user.set_password(password)

            db.session.add(new_user)
            db.session.commit()

            flash("Cadastro realizado com sucesso!","success")
            print("Cadastro realizado com sucesso")

            return redirect(url_for("main.login"))

        except IntegrityError as e:
            db.session.rollback()
            print(f"Erro IntegrityError:{e}")
            flash("Usuário ou e-mail já cadastrado.","danger")
            return redirect( url_for("main.register"))

        except Exception as e:
            db.session.rollback()
            print("❌ ERRO AO CRIAR USUÁRIO")
            print(e)
            flash( "Erro interno no servidor.","danger")
            return redirect(url_for("main.register"))

    return render_template(
        "conta/register.html"
    )


# ── Login ─────────────────────────────────────────────────────────────────────
@main_blueprint.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email", "").strip().lower())
        password = (request.form.get("password", "").strip())

        if not email or not password:
            flash("Preencha todos os campos.","danger")
            return redirect(url_for("main.login"))

        try:
            user = User.query.filter_by(email=email).first()

        except Exception as e:
            print(f"Erro ao buscar usuário:{e}")

            flash("Erro interno no servidor.","danger")
            return redirect(url_for("main.login"))

        if not user:
            flash("Credenciais inválidas.","danger")
            return redirect(url_for("main.login"))

        if not user.check_password(password):
            flash("Credenciais inválidas.","danger")

            return redirect(url_for("main.login"))

        login_user(user,remember="remember" in request.form)
        flash("Login realizado com sucesso!","success")
        return redirect(url_for("main.conta"))

    return render_template("conta/login.html")



# ── Logout ────────────────────────────────────────────────────────────────────

@main_blueprint.route('/logout')
@login_required
def logout():
    logout_user()
    flash("Você saiu da sua conta.", "info")
    return redirect(url_for('main.home'))


# ── Editar conta ──────────────────────────────────────────────────────────────

@main_blueprint.route('/edit_account', methods=['GET', 'POST'])
@login_required
def edit_account():
    if request.method == 'POST':
        new_username = request.form.get('username')
        new_email = request.form.get('email')

        if new_username and new_email:
            current_user.name = new_username   # corrigido: .name
            current_user.email = new_email
            db.session.commit()
            flash('Informações atualizadas com sucesso!', 'success')
            return redirect(url_for('main.conta'))   # corrigido: main.conta
        else:
            flash('Por favor, preencha todos os campos.', 'danger')

    return render_template(
        'conta/edit_account.html',
        username=current_user.name,   # corrigido: .name
        email=current_user.email,
    )


# ── Deletar conta ─────────────────────────────────────────────────────────────

@main_blueprint.route('/delete_account', methods=['POST'])
@login_required
def delete_account():
    try:
        db.session.delete(current_user)
        db.session.commit()
        flash('Conta excluída com sucesso!', 'success')
        return redirect(url_for('main.login'))
    except Exception as e:
        flash('Ocorreu um erro ao tentar excluir a conta. Tente novamente.', 'danger')
        return redirect(url_for('main.conta'))   # corrigido: main.conta


# ── Institucionais ────────────────────────────────────────────────────────────

@main_blueprint.route('/politica_privacidade')
def politica_privacidade():
    return render_template('institucional/politica_privacidade.html')


@main_blueprint.route('/sobre')
def sobre():
    return render_template('institucional/sobre.html')