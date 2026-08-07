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
    User,
    Product,
    Promotion,
    ContactInfo,
    SocialLink,
    Cart,
    CartItem,
    HeroSlide,
    Testimonial,
    NewsletterSubscriber,
    Favorite,
    SiteConfig,
    Address,
    Coupon,
)
from loja.modules.enviar_email import enviar_email
from config import Config

from loja.modules.models import Order
from loja.modules.tracking_status import (
    TRACKING_STATUS,
    get_tracking_status,
)

from loja.modules.getway_mercado_pago.payment_service import (
    calculate_shipping,
    money,
)

tracking_blueprint = Blueprint(
    "tracking",
    __name__,
    url_prefix="/acompanhar-pedido"
)


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
VALID_CART_SIZES = VALID_PRODUCT_SIZES | {"UNICO", "ÚNICO"}


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
    """Retorna os itens como lista para relationships lazy ou dynamic."""
    if cart is None:
        return []

    items = cart.items
    return items.all() if hasattr(items, "all") else list(items)


def _cart_item_size_field(item_model):
    """
    Prefere o campo canônico selected_size.

    O model atual também possui a property size por compatibilidade com
    templates antigos, mas selected_size é a coluna persistida.
    """
    if hasattr(item_model, "selected_size"):
        return "selected_size"
    if hasattr(item_model, "size"):
        return "size"
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

        session.pop("checkout_cart", None)
        session.pop("checkout_coupon", None)
        session.modified = True

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

        session.pop("checkout_cart", None)
        session.pop("checkout_coupon", None)
        session.modified = True

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
def _get_user_cart():
    """Carrinho persistido pertencente ao usuário autenticado."""
    return Cart.query.filter_by(
        user_id=current_user.id,
    ).first()


def _get_user_cart_item(item_id):
    """
    Busca um item somente quando ele pertence ao carrinho do usuário atual.

    Evita que um usuário altere/remova um CartItem de outra conta apenas
    conhecendo o UUID.
    """
    return (
        CartItem.query
        .join(Cart, CartItem.cart_id == Cart.id)
        .filter(
            CartItem.id == item_id,
            Cart.user_id == current_user.id,
        )
        .first_or_404()
    )


def _invalidate_cart_checkout():
    """Invalida dados derivados quando o conteúdo do carrinho muda."""
    session.pop("checkout_cart", None)
    session.pop("checkout_coupon", None)
    session.modified = True


def _calculate_cart_subtotal(cart_items):
    return money(
        sum(
            (
                money(item.unit_price) * int(item.quantity)
                for item in cart_items
            ),
            Decimal("0.00"),
        )
    )


def _calculate_checkout_totals(subtotal, discount=Decimal("0.00")):
    subtotal = money(subtotal)
    discount = money(discount)

    if subtotal <= 0:
        shipping = money(Decimal("0.00"))
    else:
        shipping = money(calculate_shipping(subtotal))

    total = money(
        max(
            subtotal + shipping - discount,
            Decimal("0.00"),
        )
    )

    return shipping, total


def _user_already_completed_purchase():
    """
    Usado pelos cupons de primeira compra.

    Mantém a mesma regra já usada pela integração de pagamento:
    pedidos pagos ou encaminhados para revisão manual contam como compra.
    """
    return (
        Order.query
        .filter(
            Order.user_id == current_user.id,
            Order.status.in_(["paid", "manual_review"]),
        )
        .first()
        is not None
    )


def _restore_checkout_coupon(
    checkout_type,
    subtotal,
    *,
    product_id=None,
):
    """
    Recupera o cupom salvo na sessão e o revalida no servidor.

    Isso permite atualizar a página de checkout sem perder visualmente
    o cupom, mas nunca confia somente no conteúdo da sessão.
    """
    saved = session.get("checkout_coupon") or {}

    if saved.get("checkout_type") != checkout_type:
        return None, money(Decimal("0.00"))

    if checkout_type == "product":
        saved_product_id = str(saved.get("product_id") or "")
        if saved_product_id != str(product_id or ""):
            return None, money(Decimal("0.00"))

    code = "".join(
        str(saved.get("code") or "")
        .strip()
        .upper()
        .split()
    )

    if not code:
        return None, money(Decimal("0.00"))

    coupon = (
        Coupon.query
        .filter(db.func.upper(Coupon.code) == code)
        .first()
    )

    if coupon is None or not coupon.is_available(subtotal):
        session.pop("checkout_coupon", None)
        session.modified = True
        return None, money(Decimal("0.00"))

    if coupon.first_purchase_only and _user_already_completed_purchase():
        session.pop("checkout_coupon", None)
        session.modified = True
        return None, money(Decimal("0.00"))

    discount = money(
        coupon.calculate_discount(subtotal)
    )

    return coupon, discount


def _validate_cart_for_checkout(cart):
    """
    Valida o carrinho no servidor antes de abrir o checkout.

    Confirma produto, quantidade, tamanho, estoque e valor do item.
    """
    cart_items = _cart_items_as_list(cart)

    if not cart_items:
        raise ValueError("Seu carrinho está vazio.")

    for item in cart_items:
        product = Product.query.filter_by(
            id=item.product_id,
            is_active=True,
        ).first()

        if product is None:
            raise ValueError(
                "Um dos produtos do carrinho não está mais disponível."
            )

        try:
            quantity = int(item.quantity)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"A quantidade de {product.name} é inválida."
            ) from error

        if quantity < 1 or quantity > 99:
            raise ValueError(
                f"A quantidade de {product.name} é inválida."
            )

        try:
            unit_price = money(item.unit_price)
        except Exception as error:
            raise ValueError(
                f"O valor de {product.name} é inválido."
            ) from error

        if unit_price < 0:
            raise ValueError(
                f"O valor de {product.name} é inválido."
            )

        stock = getattr(product, "stock", None)

        if stock is not None:
            if stock <= 0:
                raise ValueError(
                    f"{product.name} está sem estoque."
                )

            if quantity > stock:
                raise ValueError(
                    f"A quantidade de {product.name} excede o estoque. "
                    f"Disponível: {stock}."
                )

        selected_size = (
            getattr(item, "selected_size", None)
            or getattr(item, "size", None)
            or ""
        )

        selected_size = str(selected_size).strip().upper()

        if selected_size not in VALID_CART_SIZES:
            raise ValueError(
                f"O tamanho selecionado para {product.name} é inválido."
            )

    return cart_items


@main_blueprint.route("/cart")
@login_required
def carrinho():
    """Exibe o carrinho e o resumo usado pelo template cart/carrinho.html."""
    cart = _get_user_cart()
    cart_items = _cart_items_as_list(cart)

    subtotal = _calculate_cart_subtotal(cart_items)

    if cart_items:
        shipping, total = _calculate_checkout_totals(subtotal)
    else:
        shipping = money(Decimal("0.00"))
        total = money(Decimal("0.00"))

    return render_template(
        "cart/carrinho.html",
        cart=cart,
        cart_items=cart_items,
        subtotal=subtotal,
        shipping=shipping,
        total=total,
    )


@main_blueprint.post("/cart/item/<uuid:item_id>/quantidade")
@login_required
def atualizar_quantidade_carrinho(item_id):
    """
    Endpoint usado pelos botões + e - de cart/carrinho.html.
    """
    item = _get_user_cart_item(item_id)
    action = (request.form.get("action") or "").strip().lower()

    if action not in {"increase", "decrease"}:
        flash("Ação de quantidade inválida.", "danger")
        return redirect(url_for("main.carrinho"))

    product = Product.query.filter_by(
        id=item.product_id,
        is_active=True,
    ).first()

    if product is None:
        flash(
            "Este produto não está mais disponível.",
            "warning",
        )
        return redirect(url_for("main.carrinho"))

    current_quantity = int(item.quantity or 0)

    if action == "decrease":
        if current_quantity <= 1:
            flash(
                "A quantidade mínima é 1. Use Remover produto para excluir o item.",
                "info",
            )
            return redirect(url_for("main.carrinho"))

        item.quantity = current_quantity - 1

    else:
        new_quantity = current_quantity + 1

        if new_quantity > 99:
            flash(
                "A quantidade máxima permitida é 99.",
                "warning",
            )
            return redirect(url_for("main.carrinho"))

        stock = getattr(product, "stock", None)

        if stock is not None and new_quantity > stock:
            flash(
                f"Há somente {stock} unidade(s) de {product.name} em estoque.",
                "warning",
            )
            return redirect(url_for("main.carrinho"))

        item.quantity = new_quantity

    try:
        cart = item.cart

        if hasattr(cart, "updated_at"):
            cart.updated_at = datetime.utcnow()

        db.session.commit()
        _invalidate_cart_checkout()

        return redirect(url_for("main.carrinho"))

    except Exception as error:
        db.session.rollback()
        print(f"Erro ao alterar quantidade do carrinho: {error}")
        flash(
            "Não foi possível atualizar a quantidade.",
            "danger",
        )
        return redirect(url_for("main.carrinho"))


@main_blueprint.post("/cart/item/<uuid:item_id>/remover")
@login_required
def remover_item_carrinho(item_id):
    """Endpoint usado pelo botão Remover produto do carrinho."""
    item = _get_user_cart_item(item_id)

    try:
        cart = item.cart
        product_name = (
            item.product.name
            if getattr(item, "product", None)
            else "Produto"
        )

        db.session.delete(item)

        if hasattr(cart, "updated_at"):
            cart.updated_at = datetime.utcnow()

        db.session.commit()
        _invalidate_cart_checkout()

        flash(
            f"{product_name} removido do carrinho.",
            "success",
        )

    except Exception as error:
        db.session.rollback()
        print(f"Erro ao remover item do carrinho: {error}")
        flash(
            "Não foi possível remover o produto do carrinho.",
            "danger",
        )

    return redirect(url_for("main.carrinho"))


@main_blueprint.route(
    "/cart/processar",
    methods=["POST"],
)
@login_required
def processar_carrinho():
    """
    Mesmo padrão de processar_produto():

    carrinho -> validação no servidor -> sessão -> checkout.
    """
    action = (request.form.get("action") or "").strip().lower()

    if action != "checkout":
        flash("Ação de compra inválida.", "danger")
        return redirect(url_for("main.carrinho"))

    cart = _get_user_cart()

    try:
        cart_items = _validate_cart_for_checkout(cart)
    except ValueError as error:
        flash(str(error), "warning")
        return redirect(url_for("main.carrinho"))

    session.pop("checkout_product", None)
    session.pop("checkout_coupon", None)

    session["checkout_cart"] = {
        "cart_id": str(cart.id),
        "items_count": len(cart_items),
    }

    session.modified = True

    return redirect(
        url_for("main.checkout_carrinho")
    )


@main_blueprint.route(
    "/checkout/carrinho",
    methods=["GET"],
)
@login_required
def checkout_carrinho():
    """Checkout do carrinho completo."""
    cart = _get_user_cart()

    try:
        cart_items = _validate_cart_for_checkout(cart)
    except ValueError as error:
        flash(str(error), "warning")
        return redirect(url_for("main.carrinho"))

    checkout_data = session.get("checkout_cart") or {}

    if (
        checkout_data.get("cart_id")
        and checkout_data.get("cart_id") != str(cart.id)
    ):
        session.pop("checkout_cart", None)
        session.pop("checkout_coupon", None)
        session.modified = True

        flash(
            "O carrinho mudou. Revise os produtos antes de continuar.",
            "warning",
        )
        return redirect(url_for("main.carrinho"))

    subtotal = _calculate_cart_subtotal(cart_items)

    applied_coupon, discount = _restore_checkout_coupon(
        "cart",
        subtotal,
    )

    shipping, total = _calculate_checkout_totals(
        subtotal,
        discount,
    )

    return render_template(
        "cart/checkout.html",
        checkout_mode="cart",
        cart=cart,
        cart_items=cart_items,
        subtotal=subtotal,
        shipping=shipping,
        discount=discount,
        total=total,
        applied_coupon=applied_coupon,
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
    same_product = (
        checkout_data.get("product_id") == str(product.id)
    )

    if not same_product:
        flash(
            "Selecione tamanho e quantidade antes de abrir o checkout.",
            "warning",
        )
        return redirect(
            url_for(
                "main.produto_individual",
                id=product.id,
            )
        )

    quantity = _parse_product_quantity(
        checkout_data.get("quantity")
    )

    selected_size = str(
        checkout_data.get("size") or ""
    ).strip().upper()

    if quantity is None:
        flash(
            "A quantidade selecionada não é válida.",
            "warning",
        )
        return redirect(
            url_for(
                "main.produto_individual",
                id=product.id,
            )
        )

    if selected_size not in VALID_PRODUCT_SIZES:
        flash(
            "Selecione um tamanho válido.",
            "warning",
        )
        return redirect(
            url_for(
                "main.produto_individual",
                id=product.id,
            )
        )

    stock = getattr(product, "stock", None)

    if stock is not None:
        if stock <= 0:
            flash(
                "Este produto está sem estoque.",
                "warning",
            )
            return redirect(
                url_for(
                    "main.produto_individual",
                    id=product.id,
                )
            )

        if quantity > stock:
            flash(
                f"Há somente {stock} unidade(s) em estoque.",
                "warning",
            )
            return redirect(
                url_for(
                    "main.produto_individual",
                    id=product.id,
                )
            )

    unit_price = money(product.price)
    subtotal = money(
        unit_price * quantity
    )

    applied_coupon, discount = _restore_checkout_coupon(
        "product",
        subtotal,
        product_id=product.id,
    )

    shipping, total = _calculate_checkout_totals(
        subtotal,
        discount,
    )

    return render_template(
        "cart/checkout.html",
        checkout_mode="buy_now",
        product=product,
        selected_size=selected_size,
        quantity=quantity,
        unit_price=unit_price,
        subtotal=subtotal,
        shipping=shipping,
        discount=discount,
        total=total,
        applied_coupon=applied_coupon,
    )


# ── Helper do subtotal do checkout ───────────────────────────────────────────
def _get_checkout_subtotal(
    checkout_type,
    product_id=None,
    quantity=1,
):
    checkout_type = str(
        checkout_type or ""
    ).strip().lower()

    if checkout_type == "cart":
        cart = _get_user_cart()
        cart_items = _validate_cart_for_checkout(cart)
        return _calculate_cart_subtotal(cart_items)

    if checkout_type == "product":
        try:
            parsed_product_id = uuid.UUID(
                str(product_id)
            )
        except (TypeError, ValueError) as error:
            raise ValueError(
                "Produto inválido."
            ) from error

        product = Product.query.filter_by(
            id=parsed_product_id,
            is_active=True,
        ).first()

        if product is None:
            raise ValueError(
                "Produto não encontrado ou indisponível."
            )

        parsed_quantity = _parse_product_quantity(
            quantity
        )

        if parsed_quantity is None:
            raise ValueError(
                "Quantidade inválida."
            )

        stock = getattr(product, "stock", None)

        if stock is not None:
            if stock <= 0:
                raise ValueError(
                    "Produto sem estoque."
                )

            if parsed_quantity > stock:
                raise ValueError(
                    "Quantidade maior que o estoque disponível."
                )

        return money(
            money(product.price) * parsed_quantity
        )

    raise ValueError(
        "Tipo de checkout inválido."
    )


# ── Aplicar cupom no checkout ────────────────────────────────────────────────
@main_blueprint.post("/checkout/aplicar-cupom")
@login_required
def apply_checkout_coupon():
    data = request.get_json(silent=True) or {}

    code = "".join(
        str(data.get("code") or "")
        .strip()
        .upper()
        .split()
    )

    checkout_type = str(
        data.get("checkout_type") or ""
    ).strip().lower()

    if checkout_type not in {"cart", "product"}:
        return jsonify({
            "success": False,
            "message": "Tipo de checkout inválido.",
        }), 400

    if not code:
        return jsonify({
            "success": False,
            "message": "Digite um código de cupom.",
        }), 400

    try:
        subtotal = _get_checkout_subtotal(
            checkout_type=checkout_type,
            product_id=data.get("product_id"),
            quantity=data.get("quantity"),
        )
    except ValueError as error:
        return jsonify({
            "success": False,
            "message": str(error),
        }), 400

    coupon = (
        Coupon.query
        .filter(
            db.func.upper(Coupon.code) == code
        )
        .first()
    )

    if coupon is None:
        return jsonify({
            "success": False,
            "message": "Cupom não encontrado.",
        }), 404

    if not coupon.is_available(subtotal):
        return jsonify({
            "success": False,
            "message": (
                "Cupom expirado, indisponível ou inválido "
                "para este pedido."
            ),
        }), 400

    if (
        coupon.first_purchase_only
        and _user_already_completed_purchase()
    ):
        return jsonify({
            "success": False,
            "message": (
                "Este cupom é exclusivo para a primeira compra."
            ),
        }), 400

    discount = money(
        coupon.calculate_discount(subtotal)
    )

    shipping, total = _calculate_checkout_totals(
        subtotal,
        discount,
    )

    session["checkout_coupon"] = {
        "code": coupon.code,
        "checkout_type": checkout_type,
        "product_id": (
            str(data.get("product_id") or "")
            if checkout_type == "product"
            else ""
        ),
        "quantity": (
            int(data.get("quantity") or 1)
            if checkout_type == "product"
            else None
        ),
    }

    session.modified = True

    return jsonify({
        "success": True,
        "message": f"Cupom {coupon.code} aplicado com sucesso.",
        "code": coupon.code,
        "subtotal": float(subtotal),
        "shipping": float(shipping),
        "discount": float(discount),
        "total": float(total),
    }), 200


# ── Remover cupom do checkout ────────────────────────────────────────────────
@main_blueprint.post("/checkout/remover-cupom")
@login_required
def remove_checkout_coupon():
    data = request.get_json(silent=True) or {}

    checkout_type = str(
        data.get("checkout_type") or ""
    ).strip().lower()

    try:
        subtotal = _get_checkout_subtotal(
            checkout_type=checkout_type,
            product_id=data.get("product_id"),
            quantity=data.get("quantity"),
        )
    except ValueError as error:
        return jsonify({
            "success": False,
            "message": str(error),
        }), 400

    shipping, total = _calculate_checkout_totals(
        subtotal,
        Decimal("0.00"),
    )

    session.pop("checkout_coupon", None)
    session.modified = True

    return jsonify({
        "success": True,
        "message": "Cupom removido.",
        "code": "",
        "subtotal": float(subtotal),
        "shipping": float(shipping),
        "discount": 0.0,
        "total": float(total),
    }), 200


# ── Cálculo do frete ────────────────────────────────────────────────────────
@main_blueprint.route(
    "/calcular_frete",
    methods=["POST"],
)
@login_required
def calcular_frete():
    dados = request.get_json(silent=True) or {}

    cep = "".join(
        character
        for character in str(dados.get("cep") or "")
        if character.isdigit()
    )

    product_id = dados.get("product_id")
    quantity = _parse_product_quantity(
        dados.get("quantity")
    )

    if len(cep) != 8:
        return jsonify({
            "error": "Informe um CEP válido com 8 dígitos.",
        }), 400

    if not product_id:
        return jsonify({
            "error": "product_id é obrigatório.",
        }), 400

    if quantity is None:
        return jsonify({
            "error": "Quantidade inválida.",
        }), 400

    try:
        product_uuid = uuid.UUID(
            str(product_id)
        )
    except (ValueError, TypeError, AttributeError):
        return jsonify({
            "error": "product_id inválido.",
        }), 400

    product = Product.query.filter_by(
        id=product_uuid,
        is_active=True,
    ).first()

    if product is None:
        return jsonify({
            "error": "Produto não encontrado ou indisponível.",
        }), 404

    stock = getattr(product, "stock", None)

    if stock is not None and quantity > stock:
        return jsonify({
            "error": "Quantidade maior que o estoque disponível.",
        }), 400

    subtotal = money(
        money(product.price) * quantity
    )

    shipping = money(
        calculate_shipping(subtotal)
    )

    total = money(
        subtotal + shipping
    )

    return jsonify({
        "valor": f"{shipping:.2f}",
        "subtotal": f"{subtotal:.2f}",
        "total": f"{total:.2f}",
        "quantity": quantity,
        "cep": cep,
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

# ── Rastreio de pedidos ────────────────────────────────────

@tracking_blueprint.route("/", methods=["GET", "POST"])
def acompanhar_pedido():
    order = None
    error = None

    if request.method == "POST":
        order_number = (
            request.form.get("order_number", "")
            .strip()
        )

        email = (
            request.form.get("email", "")
            .strip()
            .lower()
        )

        if not order_number or not email:
            error = (
                "Informe o número do pedido e o e-mail da compra."
            )
        else:
            # Algumas versões antigas do projeto possuíam order_number.
            # O Order atual usa UUID como chave primária e pode não possuir
            # essa coluna. Suportamos as duas estruturas.
            if hasattr(Order, "order_number"):
                order = (
                    Order.query
                    .filter(
                        Order.order_number == order_number,
                        db.func.lower(Order.customer_email) == email,
                    )
                    .first()
                )
            else:
                try:
                    parsed_order_id = uuid.UUID(order_number)
                except (ValueError, TypeError, AttributeError):
                    parsed_order_id = None

                if parsed_order_id is not None:
                    order = (
                        Order.query
                        .filter(
                            Order.id == parsed_order_id,
                            db.func.lower(Order.customer_email) == email,
                        )
                        .first()
                    )

            if not order:
                error = (
                    "Não encontramos um pedido com esses dados. "
                    "Confira o número e o e-mail informados."
                )

    shipment_data = []

    if order:
        # Compatibilidade com templates de rastreio que ainda exibem
        # order.order_number.
        if not hasattr(Order, "order_number"):
            order.order_number = str(order.id)

        for shipment in order.shipments:
            status_info = get_tracking_status(
                shipment.status
            )

            shipment_data.append({
                "shipment": shipment,
                "status_info": status_info,
                "events": shipment.events,
            })

    return render_template(
        "tracking/acompanhar_pedido.html",
        order=order,
        error=error,
        shipment_data=shipment_data,
        all_statuses=TRACKING_STATUS,
    )


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