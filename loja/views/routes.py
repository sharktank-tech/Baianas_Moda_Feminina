from flask import Blueprint, render_template, flash, redirect, url_for, request, Response, jsonify
from flask_login import login_user, login_required, current_user, logout_user
from sqlalchemy.exc import IntegrityError
from loja.modules.enviar_email import enviar_email
from config import Config
from loja.modules.models import db, User,Product, ContactInfo, SocialLink, Cart
from loja.modules.services import get_home_data
from decimal import Decimal
from sqlalchemy import asc, desc

main_blueprint = Blueprint('main', __name__)

# ── Variáveis de contexto globais ────────────────────────────────────────────────────────

@main_blueprint.context_processor
def inject_global_data():
    return get_home_data()

# ── Injeção de categorias ────────────────────────────────────────────────────────────────
@main_blueprint.context_processor
def inject_categories():

    categorias = (
        db.session
        .query(Product.category)
        .filter(
            Product.is_active == True,
            Product.category.isnot(None),
            Product.category != ''
        )
        .distinct()
        .all()
    )

    categorias = [c[0] for c in categorias]
    print(categorias)
    return {
        "navbar_categories": categorias
    }

# ── Home ──────────────────────────────────────────────────────────────────────

@main_blueprint.route('/')
def home():
    return render_template("main/index.html")

# ── Produtos ──────────────────────────────────────────────────────────────────

@main_blueprint.route('/produtos')
def produtos():

    categoria = request.args.get("categoria")
    ordenar = request.args.get("ordenar", "recentes")

    query = Product.query.filter(
        Product.is_active == True
    )

    # Filtro por categoria
    if categoria:
        query = query.filter(
            Product.category == categoria
        )

    # Ordenação
    if ordenar == "menor-preco":

        query = query.order_by(
            asc(Product.price)
        )

    elif ordenar == "maior-preco":

        query = query.order_by(
            desc(Product.price)
        )

    else:

        query = query.order_by(
            desc(Product.created_at)
        )

    products = query.all()

    # Categorias para a página de produtos
    categorias = (
        db.session
        .query(Product.category)
        .filter(
            Product.is_active == True,
            Product.category.isnot(None),
            Product.category != ""
        )
        .distinct()
        .order_by(Product.category.asc())
        .all()
    )

    categorias = [
        categoria[0]
        for categoria in categorias
    ]

    return render_template(
        "main/produtos.html",
        products=products,
        categorias=categorias,
        categoria_atual=categoria,
        ordenar_atual=ordenar
    )

# ── Produto individual ───────────────────────────────────────────────

@main_blueprint.route('/produtos/<id>')
def produto_individual(id):
    produto = Product.query.get_or_404(id)
    site = get_home_data()
    related_products = Product.query.filter_by(is_active=True)


    return render_template(
        "main/produto_individual.html",
        related_products=related_products,
        product=produto,
        **site
    )

# ── Carrinho ────────────────────────────────────────────────────────────────

@main_blueprint.route('/cart')
@login_required  # Garante que o usuário está logado
def carrinho():
    site = get_home_data()
    # Busca o carrinho associado ao usuário logado
    cart = Cart.query.filter_by(user_id=current_user.id).first()

    # Se o carrinho não existir, você pode criá-lo ou retornar uma página vazia
    if not cart:
        cart_items = []
        subtotal = 0.0
        total = 0.0
    else:
        cart_items = cart.items
        subtotal = 100.0  # Substitua pela sua lógica de cálculo real
        total = subtotal  # Substitua pela sua lógica de cálculo real

    shipping = '20'

    return render_template(
        "cart/carrinho.html",
        cart_items=cart_items,
        subtotal=subtotal,
        shipping=shipping,
        total=total,
        site=site
    )

# ── Checkout ────────────────────────────────────────────────────

@main_blueprint.route('/checkout/<uuid:product_id>')
@login_required
def checkout(product_id):

    product = Product.query.get_or_404(product_id)

    quantity = 1
    subtotal = product.price

    shipping = Decimal.from_float(0)

    total = subtotal + shipping

    return render_template(
        'cart/checkout.html',
        product=product,
        quantity=quantity,
        subtotal=subtotal,
        shipping=shipping,
        total=total,
        site=get_home_data()
    )

# ── Calculo do frete ──────────────────────────────────────────

@main_blueprint.route("/calcular_frete", methods=["POST"])
@login_required
def calcular_frete():

    dados = request.get_json()

    cep = dados["cep"]
    product_id = dados["product_id"]

    produto = Product.query.get_or_404(product_id)

    # posteriormente substituir por Melhor Envio
    valor_frete = Decimal.from_float(18.90)

    total = produto.price + valor_frete

    return jsonify({
        "valor": f"{valor_frete:.2f}",
        "total": f"{total:.2f}"
    })

# ── Conta ───────────────────────────────────────────────────────────────────

@main_blueprint.route('/conta')
@login_required
def conta():

    try:
        user = current_user

        return render_template(
            'conta/conta.html',
            admin=user.is_admin,
            telefone=user.phone,
            username=user.name,
            email=user.email,
            adress=user.addresses[0] if user.addresses else None,
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
        telefone = request.form.get("password", "")
        password = request.form.get("password","")
        print(username, email, telefone, password)

        # Validação
        if not username or not email or not telefone or not password:
            flash("Todos os campos são obrigatórios.","danger")
            return redirect(url_for("main.register"))
        try:
            # Verifica usuário existente
            existing_user = User.query.filter((User.email == email) | (User.name == username)).first()

            if existing_user:
                flash("Usuário ou e-mail já cadastrado.","danger")
                return redirect(url_for("main.register"))

            # Cria usuário
            new_user = User(name=username,telefone=telefone,email=email,is_admin=False)
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

@main_blueprint.route('/condições_de_uso')
def condi_uso():
    return render_template('institucional/condi_uso.html')

# ================== Contato ==================

@main_blueprint.route("/contato", methods=["GET", "POST"])
def contato():
    info_contatos = ContactInfo.query.first()
    social_links = SocialLink.query.all()

    if request.method == "POST":
        nome = request.form.get("nome")
        email = request.form.get("email")
        assunto = request.form.get("assunto")
        mensagem = request.form.get("mensagem")

        if not all([nome, email, assunto, mensagem]):
            flash("Preencha todos os campos.", "warning")
            return redirect(url_for("main.contato"))

        conteudo_assunto = f"""
        Nome: {nome}
        E-mail: {email}
        Assunto: {assunto}
        """.strip()

        try:
            enviar_email(
                Config.EMAIL_DESTINE,
                conteudo_assunto,
                mensagem.strip()
            )

            flash("Mensagem enviada com sucesso!", "success")

        except Exception:
            flash("Erro ao enviar a mensagem.", "danger")

        return redirect(url_for("main.contato"))

    return render_template(
        "institucional/contato.html",
        contact_info=info_contatos,
        social_links=social_links
    )

# ================== Robots ==================

@main_blueprint.route("/robots.txt")
def robots():
    return Response(
        """User-agent: *\nDisallow: 
        /conta/\n
        /admin_dashboard/\n
        /admin_products/\n
        /admin/products/add/\n
        /admin/products/edit/<int:id>\n
        /admin/products/delete/<int:id>\n
        /admin/categorias/add/\n
        /admin/users/delete/<int:id>/""",
        mimetype="text/plain")