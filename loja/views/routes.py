import uuid
from decimal import Decimal
from datetime import datetime

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, Response, session
)
from flask_login import login_user, login_required, current_user, logout_user
from sqlalchemy import asc, desc
from sqlalchemy.exc import IntegrityError

from loja.modules.newsletter import subscribe_newsletter

from loja import db
from loja.modules.models import (
    User, Product, Promotion, ContactInfo, SocialLink, Cart,
    HeroSlide, Testimonial, NewsletterSubscriber, Favorite, SiteConfig, Address
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

    related_query = Product.query.filter(
        Product.is_active == True,
        Product.id != product.id,
    )
    if product.category:
        related_query = related_query.filter(Product.category == product.category)

    related_products = (
        related_query
        .order_by(Product.created_at.desc())
        .limit(4)
        .all()
    )

    return render_template(
        "main/produto_individual.html",
        product=product,
        related_products=related_products,
    )


# ── Helpers do fluxo produto → carrinho/checkout ─────────────────────────────
VALID_PRODUCT_SIZES = {"P", "M", "G", "GG"}


def _safe_internal_url(value):
    """Aceita somente caminhos internos, evitando redirecionamento externo."""
    return bool(value and value.startswith("/") and not value.startswith("//"))


def _parse_product_quantity(raw_value):
    try:
        quantity = int(raw_value)
    except (TypeError, ValueError):
        return None

    if quantity < 1 or quantity > 99:
        return None
    return quantity


def _uuid_primary_key_values(model):
    """Preenche id UUID apenas quando o model exige um UUID sem default."""
    values = {}
    id_column = model.__table__.columns.get("id")
    if id_column is None or id_column.default is not None or id_column.server_default is not None:
        return values

    try:
        if id_column.type.python_type is uuid.UUID:
            values["id"] = uuid.uuid4()
    except (AttributeError, NotImplementedError):
        pass
    return values


def _get_or_create_cart():
    cart = Cart.query.filter_by(user_id=current_user.id).first()
    if cart:
        return cart

    values = _uuid_primary_key_values(Cart)
    values["user_id"] = current_user.id
    cart = Cart(**values)
    db.session.add(cart)
    db.session.flush()
    return cart


def _cart_items_as_list(cart):
    items = cart.items
    return items.all() if hasattr(items, "all") else list(items)


def _cart_item_size_field(item_model):
    if hasattr(item_model, "size"):
        return "size"
    if hasattr(item_model, "selected_size"):
        return "selected_size"
    return None


def _add_product_to_cart(product, size, quantity):
    """
    Adiciona ou incrementa o produto no carrinho.

    O model relacionado em Cart.items precisa ter: product_id, quantity,
    unit_price e um campo size ou selected_size.
    """
    cart = _get_or_create_cart()
    item_model = Cart.items.property.mapper.class_
    size_field = _cart_item_size_field(item_model)

    if not hasattr(item_model, "product_id"):
        raise RuntimeError("O item do carrinho precisa possuir o campo product_id.")
    if not hasattr(item_model, "quantity") or not hasattr(item_model, "unit_price"):
        raise RuntimeError("O item do carrinho precisa possuir quantity e unit_price.")
    if not size_field:
        raise RuntimeError(
            "O item do carrinho precisa possuir um campo size ou selected_size."
        )

    existing_item = None
    for item in _cart_items_as_list(cart):
        item_size = getattr(item, size_field, None)
        if item.product_id == product.id and item_size == size:
            existing_item = item
            break

    new_quantity = quantity
    if existing_item:
        new_quantity += existing_item.quantity

    stock = getattr(product, "stock", None)
    if stock is not None and stock >= 0 and new_quantity > stock:
        raise ValueError(
            f"Quantidade indisponível. Há somente {stock} unidade(s) em estoque."
        )

    current_price = Decimal(str(product.price))

    if existing_item:
        existing_item.quantity = new_quantity
        existing_item.unit_price = current_price
        return existing_item

    values = _uuid_primary_key_values(item_model)
    values.update({
        "product_id": product.id,
        "quantity": quantity,
        "unit_price": current_price,
        size_field: size,
    })

    item = item_model(**values)
    cart.items.append(item)
    return item


# ── Comprar agora ou adicionar ao carrinho ───────────────────────────────────
@main_blueprint.route(
    "/produtos/<uuid:product_id>/processar",
    methods=["POST"],
)
def processar_produto(product_id):
    product = Product.query.filter_by(
        id=product_id,
        is_active=True,
    ).first_or_404()

    action = (request.form.get("action") or "").strip()
    size = (request.form.get("size") or "").strip().upper()
    quantity = _parse_product_quantity(request.form.get("quantity"))

    if action not in {"buy_now", "add_to_cart"}:
        flash("Ação de compra inválida.", "danger")
        return redirect(url_for("main.produto_individual", id=product.id))

    if size not in VALID_PRODUCT_SIZES:
        flash("Selecione um tamanho válido.", "warning")
        return redirect(url_for("main.produto_individual", id=product.id))

    if quantity is None:
        flash("Informe uma quantidade válida.", "warning")
        return redirect(url_for("main.produto_individual", id=product.id))

    stock = getattr(product, "stock", None)
    if stock is not None and stock <= 0:
        flash("Este produto está sem estoque.", "warning")
        return redirect(url_for("main.produto_individual", id=product.id))

    if stock is not None and quantity > stock:
        flash(
            f"Quantidade indisponível. Há somente {stock} unidade(s) em estoque.",
            "warning",
        )
        return redirect(url_for("main.produto_individual", id=product.id))

    if action == "buy_now":
        # Salva somente identificador, tamanho e quantidade. Nome/preço/imagem
        # são sempre recarregados do banco no checkout para evitar adulteração.
        session["checkout_product"] = {
            "product_id": str(product.id),
            "size": size,
            "quantity": quantity,
        }
        session.modified = True

        checkout_url = url_for("main.checkout", product_id=product.id)
        if not current_user.is_authenticated:
            session["post_login_next"] = checkout_url
            flash("Entre na sua conta para continuar a compra.", "info")
            return redirect(url_for("main.login"))

        return redirect(checkout_url)

    # Carrinho exige usuário porque o carrinho atual é persistido no banco.
    if not current_user.is_authenticated:
        session["pending_cart_item"] = {
            "product_id": str(product.id),
            "size": size,
            "quantity": quantity,
        }
        session["post_login_next"] = url_for("main.adicionar_carrinho_pendente")
        session.modified = True
        flash("Entre na sua conta para adicionar o produto ao carrinho.", "info")
        return redirect(url_for("main.login"))

    try:
        _add_product_to_cart(product, size, quantity)
        db.session.commit()
        flash("Produto adicionado ao carrinho.", "success")
        return redirect(url_for("main.carrinho"))
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    except Exception as exc:
        db.session.rollback()
        print(f"Erro ao adicionar produto ao carrinho: {exc}")
        flash("Não foi possível adicionar o produto ao carrinho.", "danger")

    return redirect(url_for("main.produto_individual", id=product.id))


@main_blueprint.route("/cart/adicionar-pendente")
@login_required
def adicionar_carrinho_pendente():
    pending = session.pop("pending_cart_item", None)
    if not pending:
        return redirect(url_for("main.carrinho"))

    try:
        product_id = uuid.UUID(str(pending.get("product_id")))
    except (ValueError, TypeError, AttributeError):
        flash("O produto pendente é inválido.", "danger")
        return redirect(url_for("main.carrinho"))

    product = Product.query.filter_by(id=product_id, is_active=True).first()
    size = str(pending.get("size") or "").upper()
    quantity = _parse_product_quantity(pending.get("quantity"))

    if not product or size not in VALID_PRODUCT_SIZES or quantity is None:
        flash("Não foi possível recuperar o produto selecionado.", "danger")
        return redirect(url_for("main.carrinho"))

    try:
        _add_product_to_cart(product, size, quantity)
        db.session.commit()
        flash("Produto adicionado ao carrinho.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    except Exception as exc:
        db.session.rollback()
        print(f"Erro ao adicionar produto pendente ao carrinho: {exc}")
        flash("Não foi possível adicionar o produto ao carrinho.", "danger")

    return redirect(url_for("main.carrinho"))


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


# Substitua a rota antiga por esta:
@main_blueprint.route("/newsletter", methods=["POST"])
def newsletter():
    result = subscribe_newsletter(
        request.form.get("email"),
        send_welcome_email=True,
    )

    category = "newsletter" if result.success else "danger"
    flash(result.message, category)

    return redirect(url_for("main.home") + "#newsletter")


# ── Carrinho ────────────────────────────────────────────────────────────────
@main_blueprint.route("/cart")
@login_required
def carrinho():
    cart = Cart.query.filter_by(user_id=current_user.id).first()
    cart_items = _cart_items_as_list(cart) if cart else []

    subtotal = sum(
        (Decimal(str(item.unit_price)) * item.quantity for item in cart_items),
        Decimal("0"),
    )
    shipping = Decimal("20") if cart_items else Decimal("0")
    total = subtotal + shipping

    return render_template(
        "cart/carrinho.html",
        cart=cart,
        cart_items=cart_items,
        subtotal=subtotal,
        shipping=shipping,
        total=total,
    )


# ── Checkout de compra direta ────────────────────────────────────────────────
@main_blueprint.route("/checkout/<uuid:product_id>")
@login_required
def checkout(product_id):
    product = Product.query.filter_by(
        id=product_id,
        is_active=True,
    ).first_or_404()

    checkout_data = session.get("checkout_product") or {}
    same_product = checkout_data.get("product_id") == str(product.id)

    quantity = _parse_product_quantity(
        checkout_data.get("quantity") if same_product else 1
    ) or 1
    selected_size = (
        str(checkout_data.get("size") or "").upper()
        if same_product
        else None
    )

    if selected_size not in VALID_PRODUCT_SIZES:
        selected_size = None

    stock = getattr(product, "stock", None)
    if stock is not None and stock >= 0:
        quantity = min(quantity, max(stock, 1))

    unit_price = Decimal(str(product.price))
    subtotal = unit_price * quantity
    shipping = Decimal("0")
    total = subtotal + shipping

    return render_template(
        "cart/checkout.html",
        checkout_mode="buy_now",
        product=product,
        selected_size=selected_size,
        quantity=quantity,
        unit_price=unit_price,
        subtotal=subtotal,
        shipping=shipping,
        total=total,
    )


# ── Cálculo do frete ──────────────────────────────────────────
@main_blueprint.route("/calcular_frete", methods=["POST"])
@login_required
def calcular_frete():
    dados = request.get_json(silent=True) or {}
    cep = dados.get("cep")
    product_id = dados.get("product_id")
    quantity = _parse_product_quantity(dados.get("quantity")) or 1

    if not cep or not product_id:
        return jsonify({"error": "cep e product_id são obrigatórios"}), 400

    try:
        product_uuid = uuid.UUID(str(product_id))
    except ValueError:
        return jsonify({"error": "product_id inválido"}), 400

    produto = Product.query.get_or_404(product_uuid)

    # posteriormente substituir por integração real (Melhor Envio, etc.)
    valor_frete = Decimal("18.90")
    subtotal = Decimal(str(produto.price)) * quantity
    total = subtotal + valor_frete

    return jsonify({
        "valor": f"{valor_frete:.2f}",
        "subtotal": f"{subtotal:.2f}",
        "total": f"{total:.2f}",
        "quantity": quantity,
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
    if request.method == "GET":
        next_url = request.args.get("next")
        if _safe_internal_url(next_url):
            session["post_login_next"] = next_url

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

        next_url = session.pop("post_login_next", None)
        if _safe_internal_url(next_url):
            return redirect(next_url)

        return redirect(url_for("main.conta"))

    return render_template("conta/login.html")


# ── Logout ────────────────────────────────────────────────────────────────────
@main_blueprint.route('/logout')
@login_required
def logout():
    logout_user()
    flash("Você saiu da sua conta.", "info")
    return redirect(url_for('main.home'))


# ── Editar conta e endereço ──────────────────────────────────────────────────
@main_blueprint.route('/edit_account', methods=['GET', 'POST'])
@login_required
def edit_account():
    # Prioriza o endereço marcado como padrão. Se não existir, usa o primeiro.
    address = (
        Address.query
        .filter_by(user_id=current_user.id, is_default=True)
        .order_by(Address.created_at.asc())
        .first()
    )

    if address is None:
        address = (
            Address.query
            .filter_by(user_id=current_user.id)
            .order_by(Address.created_at.asc())
            .first()
        )

    if request.method == 'POST':
        new_username = (request.form.get('username') or '').strip()
        new_email = (request.form.get('email') or '').strip().lower()
        new_phone = (request.form.get('phone') or '').strip()

        recipient_name = (request.form.get('recipient_name') or '').strip()
        address_phone = (request.form.get('address_phone') or '').strip()
        zip_code = (request.form.get('zip_code') or '').strip()
        street = (request.form.get('street') or '').strip()
        number = (request.form.get('number') or '').strip()
        complement = (request.form.get('complement') or '').strip()
        neighborhood = (request.form.get('neighborhood') or '').strip()
        city = (request.form.get('city') or '').strip()
        state = (request.form.get('state') or '').strip().upper()
        make_default = request.form.get('is_default') == '1'

        def render_edit_form(status_code=200):
            return (
                render_template(
                    'conta/edit_account.html',
                    username=new_username,
                    email=new_email,
                    phone=new_phone,
                    address=address,
                ),
                status_code,
            )

        # Nome e e-mail continuam obrigatórios para a conta.
        if not new_username or not new_email:
            flash('Nome e e-mail são obrigatórios.', 'danger')
            return render_edit_form(400)

        if '@' not in new_email:
            flash('Informe um e-mail válido.', 'danger')
            return render_edit_form(400)

        # Impede que outro usuário utilize o mesmo e-mail.
        email_in_use = (
            User.query
            .filter(
                User.email == new_email,
                User.id != current_user.id,
            )
            .first()
        )

        if email_in_use:
            flash('Este e-mail já está sendo usado por outra conta.', 'danger')
            return render_edit_form(409)

        address_values = [
            recipient_name,
            address_phone,
            zip_code,
            street,
            number,
            complement,
            neighborhood,
            city,
            state,
        ]

        has_any_address_data = any(address_values)

        # Quando já existe endereço, ele precisa permanecer completo.
        # Para um usuário sem endereço, os campos podem continuar todos vazios.
        should_save_address = address is not None or has_any_address_data

        if should_save_address:
            required_address_fields = {
                'nome do destinatário': recipient_name,
                'CEP': zip_code,
                'rua/avenida': street,
                'número': number,
                'bairro': neighborhood,
                'cidade': city,
                'estado': state,
            }

            missing_fields = [
                label
                for label, value in required_address_fields.items()
                if not value
            ]

            if missing_fields:
                flash(
                    'Preencha os campos obrigatórios do endereço: '
                    + ', '.join(missing_fields)
                    + '.',
                    'danger',
                )
                return render_edit_form(400)

            if len(state) != 2:
                flash('O estado deve ser informado com uma sigla de duas letras.', 'danger')
                return render_edit_form(400)

        try:
            current_user.name = new_username
            current_user.email = new_email
            current_user.phone = new_phone or None

            if should_save_address:
                if address is None:
                    address = Address(
                        id=uuid.uuid4(),
                        user_id=current_user.id,
                    )
                    db.session.add(address)

                    # O primeiro endereço cadastrado sempre vira o principal.
                    if not current_user.addresses:
                        make_default = True

                if make_default:
                    (
                        Address.query
                        .filter(
                            Address.user_id == current_user.id,
                            Address.id != address.id,
                        )
                        .update(
                            {Address.is_default: False},
                            synchronize_session=False,
                        )
                    )

                address.recipient_name = recipient_name
                address.phone = address_phone or None
                address.zip_code = zip_code
                address.street = street
                address.number = number
                address.complement = complement or None
                address.neighborhood = neighborhood
                address.city = city
                address.state = state
                address.is_default = (
                    make_default
                    or address.is_default
                    or len(current_user.addresses) <= 1
                )

            db.session.commit()

            flash('Conta e endereço atualizados com sucesso!', 'success')
            return redirect(url_for('main.conta'))

        except IntegrityError:
            db.session.rollback()
            flash(
                'Não foi possível salvar. Verifique se o e-mail já está cadastrado.',
                'danger',
            )
            return render_edit_form(409)

        except Exception as error:
            db.session.rollback()
            print(f'Erro ao atualizar conta/endereço: {error}')
            flash(
                'Ocorreu um erro ao salvar as alterações. Tente novamente.',
                'danger',
            )
            return render_edit_form(500)

    return render_template(
        'conta/edit_account.html',
        username=current_user.name,
        email=current_user.email,
        phone=current_user.phone,
        address=address,
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