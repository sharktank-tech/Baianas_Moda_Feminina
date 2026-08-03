import uuid
from decimal import Decimal
from datetime import datetime

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, Response
)
from flask_login import login_user, login_required, current_user, logout_user
from sqlalchemy import asc, desc
from sqlalchemy.exc import IntegrityError

from loja import db
from loja.modules.models import (
    User, Product, Promotion, ContactInfo, SocialLink, Cart,
    HeroSlide, Testimonial, NewsletterSubscriber, Favorite, SiteConfig
)
from loja.modules.enviar_email import enviar_email
from config import Config

main_blueprint = Blueprint("main", __name__)


# ── Injeção de configurações do site (title, meta description etc.) ─────────
# Roda em TODAS as páginas porque o base.html usa site.name / site.meta_description
# no <head>. É por faltar isso que dava "'site' is undefined".
@main_blueprint.context_processor
def inject_site_config():
    site = SiteConfig.query.first()
    if site is None:
        # Fallback pra não quebrar a aplicação se a tabela ainda estiver vazia
        class _SiteFallback:
            name = "Pollo Modas"
            meta_description = "Moda feminina, masculina e infantil com os melhores preços."
        site = _SiteFallback()
    return {"site": site}


# ── Injeção de categorias (navbar/menu — roda em TODAS as páginas) ───────────
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
        .order_by(Product.category.asc())
        .all()
    )
    return {"navbar_categories": [c[0] for c in categorias]}


# ── Helpers específicos da Home ───────────────────────────────────────────────
def _get_favorited_ids():
    """IDs de produtos favoritados pelo usuário logado. Anônimo = nenhum."""
    if not current_user.is_authenticated:
        return set()
    rows = (
        db.session.query(Favorite.product_id)
        .filter(Favorite.user_id == current_user.id)
        .all()
    )
    return {r[0] for r in rows}


def _get_home_categories(limit=7):
    """
    Monta os cards de categoria da home (cat-strip / categories-grid).
    Usa uma imagem representativa (o produto mais recente daquela categoria).
    """
    rows = (
        db.session
        .query(Product.category, Product.image1)
        .filter(
            Product.is_active == True,
            Product.category.isnot(None),
            Product.category != ''
        )
        .order_by(Product.category.asc(), Product.created_at.desc())
        .all()
    )
    vistas, categorias = set(), []
    for categoria, imagem in rows:
        if categoria in vistas:
            continue
        vistas.add(categoria)
        categorias.append({
            "slug": categoria,
            "label": categoria.replace("-", " ").title(),
            "image": imagem,
        })
        if len(categorias) >= limit:
            break
    return categorias


def _serialize_product(p, favoritos):
    p.is_favorited = p.id in favoritos
    return p


def get_home_data():
    """Dados usados pela home (main/index.html) e reaproveitados no
    carrinho/checkout para manter o layout base (header/footer) consistente."""
    favoritos = _get_favorited_ids()

    hero_slides = (
        HeroSlide.query
        .filter(HeroSlide.is_active == True)
        .order_by(HeroSlide.order.asc())
        .all()
    )

    # Ofertas do dia: vêm de Promotion (Product não tem sale_price/original_price)
    now = datetime.utcnow()
    promotions = (
        Promotion.query
        .filter(
            Promotion.is_active == True,
            db.or_(Promotion.ends_at.is_(None), Promotion.ends_at > now),
        )
        .order_by(Promotion.created_at.desc())
        .limit(8)
        .all()
    )
    for promo in promotions:
        promo.is_favorited = bool(promo.product_id) and promo.product_id in favoritos

    products = (
        Product.query
        .filter(Product.is_active == True)
        .order_by(Product.created_at.desc())
        .limit(8)
        .all()
    )
    products = [_serialize_product(p, favoritos) for p in products]

    testimonials = (
        Testimonial.query
        .filter(Testimonial.is_visible == True)
        .order_by(Testimonial.created_at.desc())
        .limit(3)
        .all()
    )

    ends_ats = [p.ends_at for p in promotions if p.ends_at]
    deals_end_at = min(ends_ats) if ends_ats else now.replace(
        hour=23, minute=59, second=59, microsecond=0
    )

    return {
        "hero_slides": hero_slides,
        "categories": _get_home_categories(),
        "banner": {
            "quote": "A moda é a armadura para enfrentar a vida cotidiana.",
            "cta_text": "Ver a coleção completa",
            "cta_link": url_for("main.produtos"),
        },
        "deals_end_at": deals_end_at.isoformat(),
        "promotions": promotions,
        "products": products,
        "testimonials": testimonials,
        "store_policy": {
            "exchange_days": 30,
            "max_installments": 12,
            "free_shipping_min": 249,
        },
    }


# ── Home ──────────────────────────────────────────────────────────────────────
@main_blueprint.route("/")
def home():
    return render_template("main/index.html", **get_home_data())


# ── Produtos ──────────────────────────────────────────────────────────────────
@main_blueprint.route("/produtos")
def produtos():
    categoria = request.args.get("categoria")
    ordenar = request.args.get("ordenar", "recentes")
    busca = (request.args.get("q") or "").strip()

    query = Product.query.filter(Product.is_active == True)

    if categoria:
        query = query.filter(Product.category == categoria)

    if busca:
        query = query.filter(Product.name.ilike(f"%{busca}%"))

    if ordenar == "menor-preco":
        query = query.order_by(asc(Product.price))
    elif ordenar == "maior-preco":
        query = query.order_by(desc(Product.price))
    else:
        query = query.order_by(desc(Product.created_at))

    products = query.all()

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
    categorias = [categoria[0] for categoria in categorias]

    return render_template(
        "main/produtos.html",
        products=products,
        categorias=categorias,
        categoria_atual=categoria,
        ordenar_atual=ordenar,
        busca_atual=busca,
    )


# ── Produto individual ───────────────────────────────────────────────────────
# ids são UUID no banco, não int -> usar o conversor <uuid:id>
@main_blueprint.route("/produtos/<uuid:id>")
def produto_individual(id):
    product = Product.query.filter_by(id=id, is_active=True).first_or_404()
    product.is_favorited = product.id in _get_favorited_ids()
    return render_template("main/produto.html", product=product)


# ── Favoritar (exige login, favorito persiste no banco) ──────────────────────
@main_blueprint.route("/produtos/<uuid:id>/favoritar", methods=["POST"])
@login_required
def favoritar(id):
    Product.query.filter_by(id=id).first_or_404()

    existente = Favorite.query.filter_by(
        user_id=current_user.id, product_id=id
    ).first()

    if existente:
        db.session.delete(existente)
        is_favorited = False
    else:
        db.session.add(Favorite(id=uuid.uuid4(), user_id=current_user.id, product_id=id))
        is_favorited = True

    db.session.commit()
    return jsonify({"is_favorited": is_favorited})


# ── Newsletter ────────────────────────────────────────────────────────────────
@main_blueprint.route("/newsletter", methods=["POST"])
def newsletter():
    email = (request.form.get("email") or "").strip().lower()

    if not email or "@" not in email:
        flash("Informe um e-mail válido.", "newsletter")
        return redirect(url_for("main.home") + "#newsletter")

    ja_existe = NewsletterSubscriber.query.filter_by(email=email).first()
    if not ja_existe:
        db.session.add(NewsletterSubscriber(id=uuid.uuid4(), email=email))
        db.session.commit()

    flash("Cadastro feito! Confira seu e-mail para o cupom de 10%.", "newsletter")
    return redirect(url_for("main.home") + "#newsletter")


# ── Carrinho ────────────────────────────────────────────────────────────────
@main_blueprint.route('/cart')
@login_required
def carrinho():
    site = get_home_data()
    cart = Cart.query.filter_by(user_id=current_user.id).first()

    if not cart or not cart.items:
        cart_items = []
        subtotal = Decimal("0")
    else:
        cart_items = cart.items
        subtotal = sum((item.unit_price * item.quantity for item in cart_items), Decimal("0"))

    shipping = Decimal("20")
    total = subtotal + shipping if cart_items else Decimal("0")

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
    shipping = Decimal("0")
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


# ── Cálculo do frete ──────────────────────────────────────────
@main_blueprint.route("/calcular_frete", methods=["POST"])
@login_required
def calcular_frete():
    dados = request.get_json(silent=True) or {}
    cep = dados.get("cep")
    product_id = dados.get("product_id")

    if not cep or not product_id:
        return jsonify({"error": "cep e product_id são obrigatórios"}), 400

    try:
        product_uuid = uuid.UUID(str(product_id))
    except ValueError:
        return jsonify({"error": "product_id inválido"}), 400

    produto = Product.query.get_or_404(product_uuid)

    # posteriormente substituir por integração real (Melhor Envio, etc.)
    valor_frete = Decimal("18.90")
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
@main_blueprint.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        telefone = request.form.get("telefone", "")
        password = request.form.get("password", "")

        if not username or not email or not telefone or not password:
            flash("Todos os campos são obrigatórios.", "danger")
            return redirect(url_for("main.register"))

        try:
            existing_user = User.query.filter(
                (User.email == email) | (User.name == username)
            ).first()

            if existing_user:
                flash("Usuário ou e-mail já cadastrado.", "danger")
                return redirect(url_for("main.register"))

            # CORRIGIDO: User não tem campo "telefone", o campo real é "phone"
            new_user = User(name=username, phone=telefone, email=email, is_admin=False)
            new_user.set_password(password)

            db.session.add(new_user)
            db.session.commit()

            flash("Cadastro realizado com sucesso!", "success")
            return redirect(url_for("main.login"))

        except IntegrityError as e:
            db.session.rollback()
            print(f"Erro IntegrityError: {e}")
            flash("Usuário ou e-mail já cadastrado.", "danger")
            return redirect(url_for("main.register"))

        except Exception as e:
            db.session.rollback()
            print(f"❌ ERRO AO CRIAR USUÁRIO: {e}")
            flash("Erro interno no servidor.", "danger")
            return redirect(url_for("main.register"))

    return render_template("conta/register.html")


# ── Login ─────────────────────────────────────────────────────────────────────
@main_blueprint.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()

        if not email or not password:
            flash("Preencha todos os campos.", "danger")
            return redirect(url_for("main.login"))

        try:
            user = User.query.filter_by(email=email).first()
        except Exception as e:
            print(f"Erro ao buscar usuário: {e}")
            flash("Erro interno no servidor.", "danger")
            return redirect(url_for("main.login"))

        if not user or not user.check_password(password):
            flash("Credenciais inválidas.", "danger")
            return redirect(url_for("main.login"))

        login_user(user, remember="remember" in request.form)
        flash("Login realizado com sucesso!", "success")
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
            current_user.name = new_username
            current_user.email = new_email
            db.session.commit()
            flash('Informações atualizadas com sucesso!', 'success')
            return redirect(url_for('main.conta'))
        else:
            flash('Por favor, preencha todos os campos.', 'danger')

    return render_template(
        'conta/edit_account.html',
        username=current_user.name,
        email=current_user.email,
    )


# ── Deletar conta ─────────────────────────────────────────────────────────────
@main_blueprint.route('/delete_account', methods=['POST'])
@login_required
def delete_account():
    try:
        # CORRIGIDO: current_user é um LocalProxy do Flask-Login, não a
        # instância real do SQLAlchemy. Passar o proxy direto pro
        # session.delete() é frágil; pegamos o objeto real com
        # _get_current_object() antes de deletar.
        user = current_user._get_current_object()
        logout_user()
        db.session.delete(user)
        db.session.commit()
        flash('Conta excluída com sucesso!', 'success')
        return redirect(url_for('main.login'))
    except Exception as e:
        db.session.rollback()
        print(f"Erro ao excluir conta: {e}")
        flash('Ocorreu um erro ao tentar excluir a conta. Tente novamente.', 'danger')
        return redirect(url_for('main.conta'))


# ── Institucionais ────────────────────────────────────────────────────────────
@main_blueprint.route('/politica-privacidade')
def politica_privacidade():
    return render_template('institucional/politica_privacidade.html')


@main_blueprint.route('/sobre')
def sobre():
    return render_template('institucional/sobre.html')


# CORRIGIDO: URLs com acentos podem causar problemas de encoding/links
# quebrados em alguns clientes. Trocado para uma versão sem acento.
@main_blueprint.route('/condicoes-de-uso')
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
            enviar_email(Config.EMAIL_DESTINE, conteudo_assunto, mensagem.strip())
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
    linhas = [
        "User-agent: *",
        "Disallow: /conta/",
        "Disallow: /admin_dashboard/",
        "Disallow: /admin_products/",
        "Disallow: /admin/products/add/",
        "Disallow: /admin/products/edit/",
        "Disallow: /admin/products/delete/",
        "Disallow: /admin/categorias/add/",
        "Disallow: /admin/users/delete/",
    ]
    return Response("\n".join(linhas), mimetype="text/plain")