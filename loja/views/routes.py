import uuid
from decimal import Decimal

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, Response, session, current_app, abort, make_response
)
from flask_login import login_user, login_required, current_user, logout_user
from sqlalchemy import asc, desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from loja.modules.newsletter import subscribe_newsletter
from loja.services.product_content import (
    build_product_gallery,
    product_description_excerpt,
    product_description_image_urls,
    sanitize_product_description_html,
    sanitize_product_description_without_images,
)
from loja.services.purchase_controls import assert_purchase_allowed
from loja.services.variant_attributes import (
    VARIANT_LABEL_MAX_LEN,
    variant_attribute_value,
    variant_dim_value,
)
from loja.services.seo import (
    listing_meta,
    product_canonical_url,
    product_structured_data,
)
from loja.modules.services import (
    get_favorited_ids,
    get_footer_data,
    get_home_data,
    public_product_query,
)
from loja.cache import get_cached, set_cached
from loja.utils import utcnow

from loja import db
from loja.modules.models import (
    User,
    Product,
    ProductVariant,
    ContactInfo,
    SocialLink,
    Cart,
    CartItem,
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
from loja.services.customer_tracking import build_customer_tracking
from loja.services.order_payment_evidence import payment_is_confirmed

from loja.modules.getway_mercado_pago.payment_service import money
from loja.services.shipping_quote import estimate_label, temporary_flat_rate

tracking_blueprint = Blueprint(
    "tracking",
    __name__,
    url_prefix="/acompanhar-pedido"
)


main_blueprint = Blueprint("main", __name__)


# ── Origem de tráfego (FASE 9.0.0) ──────────────────────────────────────────
# First-touch: captura sanitizada de UTM/referrer na primeira página da sessão
# e mantém até o checkout. Somente leitura, fail-closed, nunca loga payload.
from loja.services.acquisition_source import capture_acquisition
from loja.services.acquisition_sessions import (
    record_acquisition_session,
    should_count_acquisition_session,
)


@main_blueprint.before_request
def capture_acquisition_origin():
    acquisition = capture_acquisition(
        request,
        session=session,
    )
    if should_count_acquisition_session(request):
        try:
            record_acquisition_session(acquisition, session=session)
        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "Falha ao incrementar métrica agregada de aquisição."
            )
    return None


# ── Injeção de configurações do site (title, meta description etc.) ─────────
# Roda em todos os templates da aplicação porque o base.html usa
# site.name / site.meta_description no <head>. Usamos app_context_processor
# para também atender templates de outros blueprints, como tracking/admin.
@main_blueprint.app_context_processor
def inject_site_config():
    from types import SimpleNamespace

    site = get_cached("site_config")
    if site is None:
        row = SiteConfig.query.first()
        if row is None:
            # Fallback pra não quebrar a aplicação se a tabela ainda estiver vazia
            class _SiteFallback:
                name = "Pollo Modas"
                meta_description = "Moda feminina, masculina e infantil com os melhores preços."
            site = _SiteFallback()
        else:
            # Snapshot somente-leitura: nunca cachear o ORM vivo entre
            # requests — a instância desvincula da sessão e quebra o render
            # seguinte com DetachedInstanceError.
            site = SimpleNamespace(
                name=row.name, meta_description=row.meta_description
            )
            set_cached("site_config", site, ttl=600)
    return {"site": site}


# ── Injeção global de categorias (navbar/menu) ───────────────────────────────
@main_blueprint.app_context_processor
def inject_categories():
    cached = get_cached("navbar_categories")
    if cached is not None:
        return {"navbar_categories": cached}

    categorias = (
        db.session
        .query(Product.category)
        .filter(
            Product.is_active == True,  # noqa: E712
            Product.is_draft.is_(False),
            Product.category.isnot(None),
            Product.category != ''
        )
        .distinct()
        .order_by(Product.category.asc())
        .all()
    )
    result = [c[0] for c in categorias]
    set_cached("navbar_categories", result, ttl=600)
    return {"navbar_categories": result}


# ── Injeção global do rodapé (base.html) ─────────────────────────────────────
@main_blueprint.app_context_processor
def inject_footer():
    return {"footer": get_footer_data()}


# ── Injeção global de helpers de conteúdo seguro (descrição de produto) ──────
@main_blueprint.app_context_processor
def inject_product_content_helpers():
    """Disponibiliza sanitização/excerpt de descrição a todos os templates.

    Corpo da página: `sanitize_product_description_html(...)` — o resultado é
    um Markup JÁ limpo, mas os templates continuam SEM usar `|safe` (autoescape
    padrão do Jinja sobre o conteúdo já sanitizado).
    Reveal/meta/cards: `product_description_excerpt(...)` — texto puro sem HTML.
    """
    from loja.utils import format_deadline_sp

    return {
        "sanitize_product_description_html": sanitize_product_description_html,
        "sanitize_product_description_without_images": sanitize_product_description_without_images,
        "product_description_excerpt": product_description_excerpt,
        "product_description_image_urls": product_description_image_urls,
        "variant_dim_value": variant_dim_value,
        "product_canonical_url": product_canonical_url,
        # Prazos de promoção sempre em America/Sao_Paulo (banco guarda UTC naive).
        "format_deadline_sp": format_deadline_sp,
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

    query = public_product_query()

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
            Product.is_active == True,  # noqa: E712
            Product.is_draft.is_(False),
            Product.category.isnot(None),
            Product.category != ""
        )
        .distinct()
        .order_by(Product.category.asc())
        .all()
    )
    categorias = [categoria[0] for categoria in categorias]

    seo = listing_meta(categoria=categoria, busca=busca)

    return render_template(
        "main/produtos.html",
        products=products,
        categorias=categorias,
        categoria_atual=categoria,
        ordenar_atual=ordenar,
        busca_atual=busca,
        seo_title=seo["title"],
        seo_description=seo["meta_description"],
        seo_robots=seo["robots"],
        seo_canonical=seo["canonical"],
    )


# ── Produto individual ───────────────────────────────────────────────────────
# ids são UUID no banco, não int -> usar o conversor <uuid:id>
@main_blueprint.route("/produtos/<uuid:id>")
def produto_individual(id):
    product = public_product_query().filter(Product.id == id).first_or_404()
    product.is_favorited = product.id in get_favorited_ids()

    selector = _build_variant_selector(product)
    uses_variants = selector["uses_variants"]
    no_variants_available = bool(
        uses_variants
        and not selector["variants"]
    )

    related_query = public_product_query().filter(
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

    gallery_images = build_product_gallery(product)

    structured_data = product_structured_data(
        product,
        gallery_images=gallery_images,
    )

    return render_template(
        "main/produto_individual.html",
        product=product,
        related_products=related_products,
        selector=selector,
        uses_variants=uses_variants,
        no_variants_available=no_variants_available,
        gallery_images=gallery_images,
        structured_data=structured_data,
    )


# ── Helpers do fluxo produto → carrinho/checkout ─────────────────────────────
VALID_PRODUCT_SIZES = {"P", "M", "G", "GG"}
VALID_CART_SIZES = VALID_PRODUCT_SIZES | {"UNICO", "ÚNICO"}


def _active_variants_for_product(product):
    """Variantes ATIVAS e com estoque de um produto, com dados seguros.

    Nunca expõe supplier_sku_id/supplier_product_id/external_sku_id ao HTML.
    Produtos que não usam variantes (ex.: internos/legados) retornam [].
    A ordenação segue cor/size para uma interface estável.
    """
    if not product.variants:
        return []

    variants = [
        v for v in product.variants
        if getattr(v, "is_active", False) and int(v.stock or 0) > 0
    ]

    def _sort_key(v):
        return (v.color or "", v.size or "")

    # Rótulos resolvidos com fallback para attributes: corrige a exibição de
    # produtos existentes sem reimportação e sem UPDATE em massa (as colunas
    # color/size continuam NULL no banco; só a apresentação resolve).
    resolved = []
    for v in sorted(variants, key=_sort_key):
        color = variant_dim_value(v, "color") or ""
        size = variant_dim_value(v, "size") or ""
        resolved.append(
            {
                "id": str(v.id),
                "color": color,
                "size": size,
                "stock": int(v.stock or 0),
                "attributes": getattr(v, "attributes", None),
                "label": " ".join(
                    part for part in (color, size) if part
                ).strip()
                or "Único",
            }
        )
    return resolved


def _variant_attr_value(attributes, wanted_key):
    """Busca o valor de um atributo AliExpress (Cor/Tamanho) em `attributes`.

    Delegação pura ao helper compartilhado (listas e dicts normalizados lá).
    """
    return variant_attribute_value(attributes, wanted_key)


def _variant_dim_label(meta, key):
    """Label de uma dimensão (color/size) para uma variante, com fallback ao
    dict de atributos AliExpress. Nunca expõe SKUs externos."""
    raw = (meta.get(key) or "").strip()
    if raw:
        return raw
    attr_val = _variant_attr_value(meta.get("attributes"), key)
    if attr_val:
        return attr_val
    return None


def _distinct_ordered(values):
    seen = []
    for value in values:
        if value is not None and value not in seen:
            seen.append(value)
    return seen


def _build_variant_selector(product):
    """Estrutura de seleção por dimensões (Cor/Tamanho) para o storefront.

    Garante que cada combinação renderizada resulte em EXATAMENTE um variant_id:

      * Cada `variant.dims[i]` pertence a `dimensions[i].options`;
      * As tuplas `dims` são únicas entre variantes (nunca há colisão fazendo o
        cliente usar apenas `matchingVariants(...)[0]`);
      * Não exige traduzir cor/tamanho ausente em um valor impossível: uma
        variante sem tamanho ganha um rótulo "Opção N" que também entra nas
        options daquela dimensão (cenário misto / dimensão parcialmente ausente).
      * Nunca expõe supplier_sku_id/external_sku_id/supplier_product_id.
    """
    metas = _active_variants_for_product(product)
    uses_variants = _uses_variants(product)
    selector_base = {
        "uses_variants": uses_variants,
        "dimensions": [],
        "variants": [],
    }
    if not metas:
        # AliExpress com variantes porém nenhuma ativa/em estoque: o storefront
        # deve exibir o estado "sem variações disponíveis", não o fluxo interno
        # por tamanho.
        return selector_base

    for meta in metas:
        meta["color_label"] = _variant_dim_label(meta, "color")
        meta["size_label"] = _variant_dim_label(meta, "size")

    color_vals = _distinct_ordered(meta["color_label"] for meta in metas)
    size_vals = _distinct_ordered(meta["size_label"] for meta in metas)

    dims_spec = []
    if color_vals:
        dims_spec.append(("color", "Cor"))
    if size_vals:
        dims_spec.append(("size", "Tamanho"))
    if not dims_spec:
        dims_spec.append(("option", "Opção"))

    # Passo 1: dimensões + valores por variante, com fallback "Opção N" para
    # slots ausentes (contador por dimensão -> valores únicos dentro da dim e
    # presentes nas options, satisfazendo a asserção de defesa).
    fallback_count = {di: 0 for di in range(len(dims_spec))}
    raw_dims = []  # lista (por variante) de listas de valores

    for meta in metas:
        values = []
        for di, (key, title) in enumerate(dims_spec):
            if key == "option":
                values.append("Opção %d" % (len(raw_dims) + 1))
                continue
            value = _variant_dim_label(meta, key)
            if not value:
                fallback_count[di] += 1
                value = "Opção %d" % fallback_count[di]
            values.append(value)
        raw_dims.append(values)

    dimensions = []
    for di, (key, title) in enumerate(dims_spec):
        slot_values = _distinct_ordered(row[di] for row in raw_dims)
        dimensions.append({"key": key, "title": title, "options": slot_values})

    variants = [
        {
            "id": meta["id"],
            "stock": meta["stock"],
            "label": meta["label"],
            "dims": raw_dims[idx],
        }
        for idx, meta in enumerate(metas)
    ]

    # Passo 2: garante unicidade das tuplas. Se duas variantes geraram o mesmo
    # dims (ex.: A=Rosa/M e B=Rosa/M), adiciono uma dimensão "Opção" segura
    # (valores únicos por variante, ligados aos UUIDs internos) para desempatar.
    # Os rótulos carregam o estoque ("Opção 2 (11987 un.)") para o cliente
    # distinguir SKUs idênticos na aparência; a identidade continua sendo o
    # variant_id — nunca há auto-seleção.
    seen_tuples = {}
    duplicated = False
    for variant in variants:
        key = tuple(variant["dims"])
        if key in seen_tuples:
            duplicated = True
            break
        seen_tuples[key] = variant

    if duplicated:
        option_labels = [
            "Opção %d (%d un.)" % (i + 1, max(int(v.get("stock") or 0), 0))
            for i, v in enumerate(variants)
        ]
        dimensions.append({"key": "option", "title": "Opção", "options": option_labels})
        for i, variant in enumerate(variants):
            variant["dims"].append(option_labels[i])

    return {
        "uses_variants": True,
        "dimensions": dimensions,
        "variants": variants,
    }


def _assert_selector_invariant(selector):
    """Asserção de defesa: válida a unicidade e a consistência do seletor.

    Para toda variante:
      * cada variant.dims[i] pertence a dimensions[i].options
      * as tuplas de dims são únicas entre variantes
      * há exatamente uma combinação renderizada por variant_id
    """
    dimensions = selector.get("dimensions", [])
    variants = selector.get("variants", [])

    seen = set()
    for variant in variants:
        dims = variant.get("dims", [])
        assert len(dims) == len(dimensions), (
            "dims sem dimensões correspondentes"
        )
        for i, value in enumerate(dims):
            assert value in dimensions[i]["options"], (
                f"valor {value!r} fora das options da dimensão {i} "
                f"({dimensions[i]['options']!r})"
            )
        key = tuple(dims)
        assert key not in seen, f"tupla de dims duplicada: {key!r}"
        seen.add(key)

    # Cada combinação renderizada precisa mapear para exatamente um variant_id.
    ids = [variant["id"] for variant in variants]
    assert len(ids) == len(set(ids)), "variant_id duplicado no seletor"


def _uses_variants(product):
    """Produtos AliExpress (e qualquer um com variantes) usam seleção por
    variante; produtos internos/legados continuam no fluxo por tamanho."""
    return (
        product.source == "aliexpress"
        or bool(_active_variants_for_product(product))
    )


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


def _add_product_to_cart(product, size=None, quantity=1, variant=None):
    """
    Adiciona ou incrementa o produto no carrinho.

    Produtos com variante (AliExpress) usam `variant` (ProductVariant) e o
    carrinho grava `variant_id`. Produtos internos/legados continuam usando
    `size`/`selected_size`. O preço de venda é SEMPRE `Product.price`.
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

    selected_variant = variant
    if selected_variant is not None and selected_variant.product_id != product.id:
        raise ValueError("A variação selecionada não pertence a este produto.")

    existing_item = None
    for item in _cart_items_as_list(cart):
        if item.product_id != product.id:
            continue

        if selected_variant is not None:
            if item.variant_id == selected_variant.id:
                existing_item = item
                break
        else:
            item_size = getattr(item, size_field, None)
            if item_size == size:
                existing_item = item
                break

    new_quantity = quantity
    if existing_item:
        new_quantity += existing_item.quantity

    if selected_variant is not None and not selected_variant.is_active:
        raise ValueError("Esta variação está indisponível.")
    stock = (
        getattr(selected_variant, "stock", None)
        if selected_variant is not None
        else getattr(product, "stock", None)
    )
    if stock is not None and stock >= 0 and new_quantity > stock:
        raise ValueError(
            f"Quantidade indisponível. Há somente {stock} unidade(s) em estoque."
        )

    current_price = Decimal(str(product.price))

    if existing_item:
        existing_item.quantity = new_quantity
        existing_item.unit_price = current_price
        return existing_item

    selected_size = size
    if selected_variant is not None:
        # Para variante, `selected_size` guarda o tamanho COMPLETO resolvido
        # (coluna ou attributes). Cor e demais dados vêm do relacionamento
        # variant_id — a identidade nunca é o texto. Acima do teto da coluna
        # (migração 0028), falha explícita: nunca truncamento silencioso.
        resolved_size = (
            variant_dim_value(selected_variant, "size") or "UNICO"
        )
        if len(resolved_size) > VARIANT_LABEL_MAX_LEN:
            raise ValueError(
                "O tamanho desta variação excede o limite suportado."
            )
        selected_size = resolved_size

    values = _uuid_primary_key_values(item_model)
    values.update({
        "product_id": product.id,
        "quantity": quantity,
        "unit_price": current_price,
        size_field: selected_size or "Único",
    })
    if selected_variant is not None and hasattr(item_model, "variant_id"):
        values["variant_id"] = selected_variant.id

    item = item_model(**values)
    cart.items.append(item)
    return item


def _resolve_selected_variant(product, variant_id):
    """Resolve e valida uma variante selecionada do produto.

    Retorna a ProductVariant ativa do produto, ou None quando o produto não usa
    variantes candidatas. Levanta ValueError para indicações inválidas. Nunca
    expõe supplier_sku_id/supplier_product_id nas mensagens.
    """
    uses_variants = _uses_variants(product)

    if not uses_variants:
        return None

    if not variant_id:
        raise ValueError("Selecione uma variação do produto.")

    try:
        variant_uuid = uuid.UUID(str(variant_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("A variação selecionada é inválida.")

    variant = (
        db.session.query(ProductVariant)
        .filter(
            ProductVariant.id == variant_uuid,
            ProductVariant.product_id == product.id,
            ProductVariant.is_active.is_(True),
        )
        .first()
    )

    if variant is None:
        raise ValueError("A variação selecionada não está mais disponível.")

    if int(variant.stock or 0) <= 0:
        raise ValueError("Esta variação está temporariamente indisponível.")

    return variant


# ── Comprar agora ou adicionar ao carrinho ───────────────────────────────────
@main_blueprint.route(
    "/produtos/<uuid:product_id>/processar",
    methods=["POST"],
)
def processar_produto(product_id):
    product = public_product_query().filter(
        Product.id == product_id,
    ).first_or_404()

    action = (request.form.get("action") or "").strip()
    size = (request.form.get("size") or "").strip().upper()
    quantity = _parse_product_quantity(request.form.get("quantity"))

    if action not in {"buy_now", "add_to_cart"}:
        flash("Ação de compra inválida.", "danger")
        return redirect(url_for("main.produto_individual", id=product.id))

    try:
        variant = _resolve_selected_variant(product, request.form.get("variant_id"))
    except ValueError as error:
        flash(str(error), "warning")
        return redirect(url_for("main.produto_individual", id=product.id))

    if variant is None and size not in VALID_PRODUCT_SIZES:
        flash("Selecione um tamanho válido.", "warning")
        return redirect(url_for("main.produto_individual", id=product.id))

    if quantity is None:
        flash("Informe uma quantidade válida.", "warning")
        return redirect(url_for("main.produto_individual", id=product.id))

    if variant is not None:
        stock = getattr(variant, "stock", None)
    else:
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
        # Salva somente identificador, variação/tamanho e quantidade. Nome,
        # preço e imagem são sempre recarregados do banco no checkout para
        # evitar adulteração.
        session["checkout_product"] = {
            "product_id": str(product.id),
            "size": size,
            "quantity": quantity,
            "variant_id": str(variant.id) if variant is not None else None,
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
            "variant_id": str(variant.id) if variant is not None else None,
        }
        session["post_login_next"] = url_for("main.adicionar_carrinho_pendente")
        session.modified = True
        flash("Entre na sua conta para adicionar o produto ao carrinho.", "info")
        return redirect(url_for("main.login"))

    try:
        _add_product_to_cart(product, size=size, quantity=quantity, variant=variant)
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
        current_app.logger.error("Erro ao adicionar produto ao carrinho: %s", exc)
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

    product = (
        public_product_query().filter(Product.id == product_id).first()
    )
    size = str(pending.get("size") or "").upper()
    quantity = _parse_product_quantity(pending.get("quantity"))

    if product is None or quantity is None:
        flash("Não foi possível recuperar o produto selecionado.", "danger")
        return redirect(url_for("main.carrinho"))

    try:
        variant = _resolve_selected_variant(product, pending.get("variant_id"))
    except ValueError as error:
        flash(str(error), "warning")
        return redirect(url_for("main.carrinho"))

    if variant is None and size not in VALID_PRODUCT_SIZES:
        flash("Não foi possível recuperar o produto selecionado.", "danger")
        return redirect(url_for("main.carrinho"))

    try:
        _add_product_to_cart(product, size=size, quantity=quantity, variant=variant)
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
        current_app.logger.error("Erro ao adicionar produto pendente ao carrinho: %s", exc)
        flash("Não foi possível adicionar o produto ao carrinho.", "danger")

    return redirect(url_for("main.carrinho"))


# ── Favoritar (exige login, favorito persiste no banco) ──────────────────────
@main_blueprint.route("/produtos/<uuid:id>/favoritar", methods=["POST"])
@login_required
def favoritar(id):
    Product.query.filter_by(id=id).first_or_404()
    public = public_product_query().filter(Product.id == id).first()
    if public is None:
        return abort(404)

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


@main_blueprint.route("/newsletter/cancelar/<token>", methods=["GET"])
def newsletter_cancelar(token):
    """Página de confirmação do cancelamento (GET não altera estado)."""
    from loja.modules.newsletter import verify_unsubscribe_token

    subscriber = verify_unsubscribe_token(token)
    return render_template(
        "main/newsletter_cancelar.html",
        valido=subscriber is not None,
        token=token,
    )


@main_blueprint.route("/newsletter/cancelar/<token>", methods=["POST"])
def newsletter_cancelar_confirmar(token):
    """Cancelamento ou reativação por ação explícita (POST)."""
    from loja.modules.newsletter import (
        reactivate_by_token,
        unsubscribe_by_token,
    )

    acao = request.form.get("acao", "cancelar")
    if acao == "reativar":
        result = reactivate_by_token(token)
    else:
        result = unsubscribe_by_token(token)

    return render_template(
        "main/newsletter_cancelar.html",
        valido=True,
        token=token,
        resultado=result,
    )


# ── Carrinho ────────────────────────────────────────────────────────────────
def get_cart_count():
    """Soma das quantidades do carrinho (fonte única, sem contagem paralela).

    Autenticado: soma `CartItem.quantity` do `Cart` do usuário. Visitante:
    quantidade do item pendente em sessão (o fluxo exige login para
    persistir o carrinho no banco). Nunca mexe em preço, estoque ou regras.
    """
    if current_user.is_authenticated:
        cart = _get_user_cart()
        return sum(int(item.quantity or 0) for item in _cart_items_as_list(cart))
    pending = session.get("pending_cart_item") or {}
    try:
        return max(0, int(pending.get("quantity") or 0))
    except (TypeError, ValueError):
        return 0


def format_cart_count(count):
    """Texto do badge: oculta no zero; acima de 99 exibe "99+"."""
    try:
        total = int(count or 0)
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        return ""
    if total > 99:
        return "99+"
    return str(total)


def cart_count_label(count):
    """Descrição acessível do ícone (não depende só da cor)."""
    try:
        total = int(count or 0)
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        return "Sacola"
    item = "item" if total == 1 else "itens"
    return f"Carrinho com {total} {item}"


@main_blueprint.app_context_processor
def inject_cart_count():
    """Disponibiliza a contagem ao cabeçalho (base.html) em toda página."""
    try:
        count = get_cart_count()
    except Exception:
        current_app.logger.warning("Falha ao contar carrinho p/ badge.",
                                   exc_info=True)
        count = 0
    return {
        "cart_count": count,
        "cart_count_display": format_cart_count(count),
        "cart_count_label": cart_count_label(count),
    }


@main_blueprint.get("/cart/count")
def cart_count_json():
    """Contagem atual em JSON (mesma fonte do badge server-render).

    Usado para corrigir o badge ao voltar pelo histórico quando o
    navegador restaura a página do bfcache. Somente leitura.
    """
    try:
        count = get_cart_count()
    except Exception:
        current_app.logger.warning("Falha ao contar carrinho (JSON).",
                                   exc_info=True)
        count = 0
    return jsonify({
        "count": count,
        "display": format_cart_count(count),
        "label": cart_count_label(count),
    })


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


def _calculate_display_totals(subtotal, discount=Decimal("0.00")):
    """Totais de vitrine (carrinho/checkout sem CEP): frete como estimativa.

    Sem destino não há cotação — retorna shipping None e o template exibe
    o rótulo de estimativa. O valor cobrado sai sempre de cotação com CEP
    (ver /calcular_frete e criação do pedido), nunca deste total parcial.
    """
    subtotal = money(subtotal)
    discount = money(discount)

    total = money(
        max(
            subtotal - discount,
            Decimal("0.00"),
        )
    )

    return None, total


def _shipping_estimate_label() -> str:
    # Flag desligada: regra comercial temporária (não é cálculo por CEP).
    from flask import current_app

    if not current_app.config.get("SHIPPING_GROUPED_QUOTE_ENABLED"):
        return "Frete padrão temporário"
    return estimate_label()


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
        product = (
            public_product_query()
            .filter(
                Product.id == item.product_id,
            )
            .with_for_update()
            .first()
        )

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

        variant = getattr(item, "variant", None)
        uses_variants = _uses_variants(product)

        if uses_variants:
            # Produto com variantes (ex.: AliExpress) NUNCA cai para o fluxo
            # legacy por selected_size. variant_id é obrigatório: sem ele,
            # uma variante removida (FK SET NULL) ou nunca preenchida deve
            # bloquear o checkout, mesmo que Product.stock > 0.
            if variant is None:
                raise ValueError(
                    f"A variação de {product.name} não foi informada."
                )

            # Ordem de lock Produto→Variante (consistente com _reserve_stock)
            # e validação cruzada de que a variante pertence ao produto.
            locked_variant = (
                ProductVariant.query
                .filter_by(id=variant.id, product_id=product.id)
                .with_for_update()
                .first()
            )

            if locked_variant is None or not locked_variant.is_active:
                raise ValueError(
                    f"Uma variação de {product.name} está indisponível."
                )
            if int(locked_variant.stock or 0) <= 0:
                raise ValueError(
                    f"Uma variação de {product.name} está sem estoque."
                )
            if quantity > int(locked_variant.stock or 0):
                raise ValueError(
                    f"A quantidade de {product.name} excede o estoque. "
                    f"Disponível: {int(locked_variant.stock or 0)}."
                )
            # Controle canônicos de NOVA compra avaliados POR VARIANTE
            # (divergência do fornecedor + origem da opção escolhida).
            assert_purchase_allowed(
                product, locked_variant, product_name=product.name
            )
            continue

        # Apenas produto realmente legacy (sem variantes) usa selected_size.
        assert_purchase_allowed(product, None, product_name=product.name)
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
        shipping, total = _calculate_display_totals(subtotal)
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
        shipping_estimate=_shipping_estimate_label(),
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

    product = public_product_query().filter(
        Product.id == item.product_id,
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

        variant = getattr(item, "variant", None)

        if variant is not None:
            stock = getattr(variant, "stock", None)
        else:
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
            cart.updated_at = utcnow()

        db.session.commit()
        _invalidate_cart_checkout()

        return redirect(url_for("main.carrinho"))

    except Exception as error:
        db.session.rollback()
        current_app.logger.error("Erro ao alterar quantidade do carrinho: %s", error)
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
            cart.updated_at = utcnow()

        db.session.commit()
        _invalidate_cart_checkout()

        flash(
            f"{product_name} removido do carrinho.",
            "success",
        )

    except Exception as error:
        db.session.rollback()
        current_app.logger.error("Erro ao remover item do carrinho: %s", error)
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


def _issue_checkout_idempotency_key():
    """Emite a chave de idempotência do checkout e a registra NA SESSÃO.

    O hidden field do form pode ser omitido/forjado no POST; a lista de
    chaves emitidas na sessão é a fonte de verdade do servidor para
    reconhecer o reenvio nesse caso (janela de 8 renderizações recentes).
    """
    key = uuid.uuid4().hex
    prior = session.get("checkout_idempotency_keys")
    issued = (
        [k for k in prior if isinstance(k, str)]
        if isinstance(prior, list)
        else []
    )
    session["checkout_idempotency_keys"] = ([key] + issued)[:8]
    return key


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

    shipping, total = _calculate_display_totals(
        subtotal,
        discount,
    )

    return render_template(
        "cart/checkout.html",
        checkout_mode="cart",
        checkout_idempotency_key=_issue_checkout_idempotency_key(),
        cart=cart,
        cart_items=cart_items,
        subtotal=subtotal,
        shipping=shipping,
        discount=discount,
        total=total,
        applied_coupon=applied_coupon,
        shipping_estimate=_shipping_estimate_label(),
    )


# ── Checkout de compra direta ────────────────────────────────────────────────
@main_blueprint.route("/checkout/<uuid:product_id>")
@login_required
def checkout(product_id):
    product = public_product_query().filter(
        Product.id == product_id,
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

    try:
        variant = _resolve_selected_variant(
            product,
            checkout_data.get("variant_id"),
        )
    except ValueError as error:
        flash(str(error), "warning")
        return redirect(
            url_for(
                "main.produto_individual",
                id=product.id,
            )
        )

    if variant is None and selected_size not in VALID_PRODUCT_SIZES:
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

    if variant is not None:
        stock = getattr(variant, "stock", None)
    else:
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

    selected_variant_label = None
    selected_variant_id = None
    if variant is not None:
        # Rótulo completo (coluna ou attributes): identidade continua sendo o
        # variant_id; o texto aqui é só apresentação, sem truncamento.
        selected_variant_label = (
            " ".join(
                part
                for part in (
                    variant_dim_value(variant, "color") or "",
                    variant_dim_value(variant, "size") or "",
                )
                if part
            ).strip()
            or "Único"
        )
        selected_variant_id = str(variant.id)

    applied_coupon, discount = _restore_checkout_coupon(
        "product",
        subtotal,
        product_id=product.id,
    )

    shipping, total = _calculate_display_totals(
        subtotal,
        discount,
    )

    return render_template(
        "cart/checkout.html",
        checkout_mode="buy_now",
        checkout_idempotency_key=_issue_checkout_idempotency_key(),
        product=product,
        selected_size=selected_size,
        selected_variant=selected_variant_label,
        selected_variant_id=selected_variant_id,
        quantity=quantity,
        unit_price=unit_price,
        subtotal=subtotal,
        shipping=shipping,
        discount=discount,
        total=total,
        applied_coupon=applied_coupon,
        shipping_estimate=_shipping_estimate_label(),
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

        product = public_product_query().filter(
            Product.id == parsed_product_id,
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

        # Produtos com variante validam estoque por variante no checkout; o
        # subtotal aqui é só financeiro (price * quantity).
        if stock is not None and not _uses_variants(product):
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

    shipping, total = _calculate_display_totals(
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
        "shipping": None,
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

    shipping, total = _calculate_display_totals(
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
        "shipping": None,
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

    from loja.services.shipping_quote import (
        QuoteUnavailable,
        ShipmentLine,
        assign_group_origins,
        canonical_items,
        default_providers,
        group_lines,
        quote_groups,
    )
    from loja.utils import utcnow

    # Compra direta (produto) ou carrinho completo: a cotação exibida fica
    # registrada na sessão (assinada) para reconfirmação no checkout.
    item_dicts = []
    subtotal = Decimal("0.00")
    if dados.get("cart"):
        cart = _get_user_cart()
        for item in _cart_items_as_list(cart):
            item_dicts.append({
                "product_id": item.product_id,
                "variant_id": getattr(item, "variant_id", None),
                "quantity": item.quantity,
            })
            subtotal += money(item.unit_price) * int(item.quantity or 1)
        if not item_dicts:
            return jsonify({
                "error": "Carrinho vazio.",
            }), 400
        quantity = None
    else:
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

        product = public_product_query().filter(
            Product.id == product_uuid,
        ).first()

        if product is None:
            return jsonify({
                "error": "Produto não encontrado ou indisponível.",
            }), 404

        stock = getattr(product, "stock", None)

        # Produtos com variante validam estoque por variante no checkout.
        if stock is not None and not _uses_variants(product) and quantity > stock:
            return jsonify({
                "error": "Quantidade maior que o estoque disponível.",
            }), 400

        item_dicts.append({
            "product_id": product.id,
            "variant_id": None,
            "quantity": quantity,
        })
        subtotal = money(
            money(product.price) * quantity
        )

    # Flag desligada (SHIPPING_GROUPED_QUOTE_ENABLED=False): tarifa comercial
    # temporária única, assumida pela loja — não é cotação por CEP.
    from flask import current_app as _current_app2
    from loja.services.shipping_quote import QuoteUnavailable as _QuoteUnavailable2

    if not _current_app2.config.get("SHIPPING_GROUPED_QUOTE_ENABLED"):
        try:
            shipping = temporary_flat_rate()
        except _QuoteUnavailable2:
            return jsonify({
                "error": "Frete temporariamente indisponível. Tente novamente em instantes.",
            }), 409
        total = money(subtotal + shipping)
        # Não grava confirmação de cotação agrupada; checkout com flag desligada
        # chama a mesma função (temporary_flat_rate).
        return jsonify({
            "valor": f"{shipping:.2f}",
            "subtotal": f"{subtotal:.2f}",
            "total": f"{total:.2f}",
            "quantity": quantity,
            "cep": cep,
            "grupos": [],
        })

    lines = [
        ShipmentLine(
            product_id=item["product_id"],
            variant_id=item.get("variant_id"),
            quantity=item["quantity"],
        )
        for item in item_dicts
    ]
    groups = assign_group_origins(group_lines(lines))
    try:
        quoted = quote_groups(groups, cep, default_providers())
    except QuoteUnavailable:
        return jsonify({
            "error": (
                "Frete indisponível para este destino no momento. "
                "Confira o CEP e tente novamente."
            ),
        }), 409

    shipping = money(quoted["total"])
    total = money(subtotal + shipping)
    quoted_at = utcnow()
    # Basta UMA remessa vencer para o total precisar de nova cotação.
    expires_at = min(g["expires_at"] for g in quoted["groups"])

    # Confirmação vinculada à sessão (assinada): CEP, itens, valor,
    # validade mínima e prazos por grupo. O checkout só gera a preferência
    # se esta confirmação ainda valer integralmente.
    session["shipping_quote"] = {
        "cep": cep,
        "items": [
            {
                "product_id": str(pid),
                "variant_id": str(vid) if vid else None,
                "quantity": qty,
            }
            for pid, vid, qty in canonical_items(item_dicts)
        ],
        "shipping": f"{shipping:.2f}",
        "quoted_at": quoted_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "groups": [
            {
                "channel": g["channel"],
                "cost": f"{g['cost']:.2f}",
                "expires_at": g["expires_at"].isoformat(),
            }
            for g in quoted["groups"]
        ],
    }
    session.modified = True

    return jsonify({
        "valor": f"{shipping:.2f}",
        "subtotal": f"{subtotal:.2f}",
        "total": f"{total:.2f}",
        "quantity": quantity,
        "cep": cep,
        "expires_at": expires_at.isoformat(),
        "grupos": [
            {
                "channel": g["channel"],
                "cost": f"{g['cost']:.2f}",
                "min_days": g["min_days"],
                "max_days": g["max_days"],
                "carrier": g["carrier"],
                "expires_at": g["expires_at"].isoformat(),
            }
            for g in quoted["groups"]
        ],
    })

# ── Encomendas / área de compras ─────────────────────────────────────────────
# Order.status representa o estado interno do pedido/pagamento.
# Shipment.status representa a etapa logística do envio.
#
# Regras usadas nos cards da conta:
#   pending_payment                              -> aguardando pagamento
#   paid + sem Shipment                          -> aguardando envio
#   paid + Shipment aguardando_postagem          -> aguardando envio
#   paid + Shipment em andamento                 -> enviado
#   paid + Shipment entregue                     -> aguardando avaliação
#
# Pedidos em manual_review continuam aparecendo em "Todas as compras", mas não
# são tratados como prontos para envio até que o pagamento seja efetivamente
# confirmado como paid.

PURCHASE_FILTERS = {
    "aguardando-pagamento": {
        "bucket": "aguardando_pagamento",
        "titulo": "Aguardando pagamento",
    },
    "aguardando-envio": {
        "bucket": "aguardando_envio",
        "titulo": "Aguardando envio",
    },
    "enviados": {
        "bucket": "enviado",
        "titulo": "Pedidos enviados",
    },
    "aguardando-avaliacao": {
        "bucket": "aguardando_avaliacao",
        "titulo": "Aguardando avaliação",
    },
    "expirados": {
        "bucket": "expirado",
        "titulo": "Pedidos expirados",
    },
}


PURCHASE_STATUS_LABELS = {
    "aguardando_pagamento": "Aguardando pagamento",
    "aguardando_envio": "Aguardando envio",
    "enviado": "Enviado",
    "aguardando_avaliacao": "Aguardando avaliação",
    "expirado": "Expirado",
}


def _normalize_status(value):
    """Normaliza chaves de status usadas pelo pedido/rastreio."""
    return (
        str(value or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )


def _shipment_is_waiting_posting(status):
    """Status logístico que ainda não representa uma postagem efetiva."""
    return _normalize_status(status) in {
        "aguardando_postagem",
        "pending_posting",
        "pending_shipment",
        "pre_transit",
        "pre_transito",
    }


def _shipment_is_delivered(status):
    """
    Detecta entrega sem acoplar a rota a uma única integração de rastreio.

    O model atual usa strings de status e o TRACKING_STATUS pode evoluir;
    por isso aceitamos a chave canônica "entregue" e variações que contenham
    "entreg"/"delivered".
    """
    normalized = _normalize_status(status)
    return "entreg" in normalized or "delivered" in normalized


def _order_fully_delivered(order, shipments) -> bool:
    """Todas as unidades vendidas cobertas por remessa ENTREGUE.

    Soma por item: cobertura de remessa entregue >= quantidade vendida em
    TODOS os itens. Sem itens ou sem cobertura total: False.
    """
    items = list(getattr(order, "items", None) or [])
    if not items:
        return False
    delivered = {}
    for shipment in shipments:
        if not _shipment_is_delivered(getattr(shipment, "status", None)):
            continue
        for link in (getattr(shipment, "coverage", None) or []):
            key = getattr(link, "order_item_id", None)
            delivered[key] = delivered.get(key, 0) + int(
                getattr(link, "quantity", 0) or 0
            )
    return all(
        delivered.get(item.id, 0) >= int(getattr(item, "quantity", 0) or 0)
        for item in items
    )


def _order_purchase_bucket(order):
    """Converte Order + Shipment para um dos quatro cards da área do cliente."""
    order_status = _normalize_status(getattr(order, "status", None))

    if order_status == "expired":
        return "expirado"

    if order_status == "pending_payment":
        return "aguardando_pagamento"

    # Só pedidos efetivamente pagos entram no fluxo logístico.
    if order_status != "paid":
        return None

    shipments = list(getattr(order, "shipments", None) or [])

    # Pedido pago, mas ainda sem objeto de rastreio criado.
    if not shipments:
        return "aguardando_envio"

    shipment_statuses = [
        _normalize_status(getattr(shipment, "status", None))
        for shipment in shipments
    ]

    # Conclusão exige cobertura ENTREGUE de todas as unidades vendidas:
    # uma remessa entregue não conclui o pedido se outra ainda aguarda
    # postagem. "Vinculado a uma remessa" != "entregue".
    if _order_fully_delivered(order, shipments):
        return "aguardando_avaliacao"

    # Todas as remessas ainda aguardam postagem.
    if all(_shipment_is_waiting_posting(status) for status in shipment_statuses):
        return "aguardando_envio"

    # Existe pelo menos uma remessa em etapa posterior à postagem.
    return "enviado"


def _get_user_orders(user_id):
    """Pedidos do usuário, mais recentes primeiro, com remessas pré-carregadas."""
    return (
        Order.query
        .options(selectinload(Order.shipments), selectinload(Order.items))
        .filter(Order.user_id == user_id)
        .order_by(Order.created_at.desc())
        .all()
    )


def _prepare_orders_for_customer(orders):
    """
    Anexa metadados somente em memória para os templates.

    Não altera o status persistido do Order.
    """
    for order in orders:
        bucket = _order_purchase_bucket(order)
        order.customer_status = bucket
        order.customer_status_label = PURCHASE_STATUS_LABELS.get(
            bucket,
            str(getattr(order, "status", "") or "Pedido").replace("_", " ").title(),
        )
    return orders


def _purchase_status_counts(orders):
    counts = {
        "aguardando_pagamento": 0,
        "aguardando_envio": 0,
        "enviado": 0,
        "aguardando_avaliacao": 0,
        "expirado": 0,
    }

    for order in orders:
        bucket = _order_purchase_bucket(order)
        if bucket in counts:
            counts[bucket] += 1

    return counts


@main_blueprint.route("/encomendas")
@login_required
def encomendas():
    try:
        pedidos = _prepare_orders_for_customer(
            _get_user_orders(current_user.id)
        )

        return render_template(
            "conta/encomendas.html",
            pedidos=pedidos,
            titulo="Todas as compras",
            filtro="todos",
        )

    except Exception as error:
        current_app.logger.error("Erro ao carregar encomendas: %s", error)
        flash("Não foi possível carregar suas compras.", "danger")
        return redirect(url_for("main.conta"))


# Rota opcional usada pelo botão "Ver pedido" do template encomendas.html.
# O template conta/detalhes_encomenda.html precisa existir para a página abrir.
@main_blueprint.route("/encomendas/pedido/<uuid:order_id>")
@login_required
def detalhes_encomenda(order_id):
    pedido = (
        Order.query
        .options(
            selectinload(Order.items),
            selectinload(Order.shipments),
            selectinload(Order.payments),
        )
        .filter(
            Order.id == order_id,
            Order.user_id == current_user.id,
        )
        .first_or_404()
    )

    _prepare_orders_for_customer([pedido])

    # Itens ainda sem cobertura total de remessa (somente contagens,
    # sem dados operacionais): entrega parcial deixa pendência explícita.
    from loja.modules.models import ShipmentItem

    covered = {}
    if pedido.shipments:
        rows = (
            db.session.query(
                ShipmentItem.order_item_id,
                db.func.coalesce(db.func.sum(ShipmentItem.quantity), 0),
            )
            .filter(
                ShipmentItem.shipment_id.in_([s.id for s in pedido.shipments])
            )
            .group_by(ShipmentItem.order_item_id)
            .all()
        )
        covered = {item_id: int(total or 0) for item_id, total in rows}
    pending_lines = [
        {"name": item.product_name, "quantity": int(item.quantity or 0) - covered.get(item.id, 0)}
        for item in (pedido.items or [])
        if int(item.quantity or 0) - covered.get(item.id, 0) > 0
    ]

    return render_template(
        "conta/detalhes_encomenda.html",
        pedido=pedido,
        payment_confirmed=payment_is_confirmed(pedido),
        pending_lines=pending_lines,
    )


# ── Refazer pedido expirado ──────────────────────────────────────────────────
@main_blueprint.route(
    "/encomendas/pedido/<uuid:order_id>/refazer",
    methods=["POST"],
)
@login_required
def refazer_pedido(order_id):
    """Reabre o checkout com os itens de um pedido com estado "expired".

    Só o dono do pedido e apenas quando o estado é "expired". Itens sem
    produto público/variante ativa/estoque são pulados com aviso; o preço
    é sempre o do catálogo atual (mesma regra do carrinho normal).
    """
    pedido = (
        Order.query
        .filter(
            Order.id == order_id,
            Order.user_id == current_user.id,
        )
        .first_or_404()
    )

    if _normalize_status(pedido.status) != "expired":
        flash("Este pedido não pode ser refeito.", "warning")
        return redirect(
            url_for("main.detalhes_encomenda", order_id=pedido.id)
        )

    adicionados = 0
    indisponiveis = 0

    for item in pedido.items:
        product = None
        if item.product_id is not None:
            product = (
                public_product_query()
                .filter(Product.id == item.product_id)
                .first()
            )

        if product is None:
            indisponiveis += 1
            continue

        variant = None
        if item.variant_id is not None:
            variant = ProductVariant.query.filter_by(
                id=item.variant_id,
                product_id=product.id,
            ).first()
            if (
                variant is None
                or not variant.is_active
                or (variant.stock is not None and variant.stock <= 0)
            ):
                indisponiveis += 1
                continue
            estoque = variant.stock
        else:
            estoque = product.stock
            if estoque is not None and estoque <= 0:
                indisponiveis += 1
                continue

        quantidade = int(item.quantity)
        if estoque is not None:
            quantidade = min(quantidade, int(estoque))

        try:
            _add_product_to_cart(
                product,
                size=item.selected_size,
                quantity=quantidade,
                variant=variant,
            )
        except (ValueError, RuntimeError):
            indisponiveis += 1
            continue

        adicionados += 1

    if not adicionados:
        db.session.rollback()
        flash(
            "Nenhum item deste pedido está disponível para uma nova compra.",
            "warning",
        )
        return redirect(
            url_for("main.detalhes_encomenda", order_id=pedido.id)
        )

    db.session.commit()

    session.pop("checkout_cart", None)
    session.pop("checkout_coupon", None)
    session.modified = True

    if indisponiveis:
        flash(
            "Alguns itens não estão mais disponíveis e ficaram de fora.",
            "warning",
        )
    else:
        flash("Itens do pedido adicionados ao carrinho.", "success")

    return redirect(url_for("main.checkout_carrinho"))


# ── Encomendas dinâmicas ─────────────────────────────────────────────────────
@main_blueprint.route("/encomendas/<status>")
@login_required
def encomendas_status(status):
    filtro = PURCHASE_FILTERS.get(status)

    if not filtro:
        flash("Categoria de pedidos inválida.", "warning")
        return redirect(url_for("main.encomendas"))

    try:
        todos_pedidos = _get_user_orders(current_user.id)
        bucket = filtro["bucket"]

        pedidos = [
            pedido
            for pedido in todos_pedidos
            if _order_purchase_bucket(pedido) == bucket
        ]

        _prepare_orders_for_customer(pedidos)

        return render_template(
            "conta/encomendas.html",
            pedidos=pedidos,
            titulo=filtro["titulo"],
            filtro=status,
        )

    except Exception as error:
        current_app.logger.error("Erro ao carregar encomendas: %s", error)
        flash("Não foi possível carregar suas compras.", "danger")
        return redirect(url_for("main.conta"))


# ── Conta ────────────────────────────────────────────────────────────────────
@main_blueprint.route("/conta")
@login_required
def conta():
    try:
        user = current_user

        # Prioriza o endereço principal, mantendo o mesmo comportamento da
        # página de edição de conta.
        address = (
            Address.query
            .filter_by(
                user_id=user.id,
                is_default=True,
            )
            .order_by(Address.created_at.asc())
            .first()
        )

        if address is None:
            address = (
                Address.query
                .filter_by(user_id=user.id)
                .order_by(Address.created_at.asc())
                .first()
            )

        pedidos = _get_user_orders(user.id)
        compras_status = _purchase_status_counts(pedidos)

        return render_template(
            "conta/conta.html",
            admin=user.is_admin,
            telefone=user.phone,
            username=user.name,
            email=user.email,
            address=address,
            compras_status=compras_status,
        )

    except Exception as error:
        flash(
            "Erro ao acessar os dados da conta. Tente novamente.",
            "danger",
        )
        current_app.logger.error("Erro ao acessar os dados da conta: %s", error)
        return redirect(url_for("main.home"))


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
            current_app.logger.error("Erro IntegrityError: %s", e)
            flash("Usuário ou e-mail já cadastrado.", "danger")
            return redirect(url_for("main.register"))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error("Erro ao criar usuário: %s", e)
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
            current_app.logger.error("Erro ao buscar usuário: %s", e)
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


# ── Esqueci minha senha ───────────────────────────────────────────────────────
def _headers_pagina_redefinicao(resp):
    """2.9: página de redefinição não vaza referência nem é cacheada."""
    resp = make_response(resp)
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _mensagem_erro_redefinicao(code: str) -> str:
    return {
        "invalid": (
            "Link inválido. Solicite um novo link de redefinição."
        ),
        "expired": (
            "Este link expirou. Solicite um novo link de redefinição."
        ),
        "used": (
            "Este link já foi utilizado. Solicite um novo link de "
            "redefinição."
        ),
        "empty": "Todos os campos são obrigatórios.",
        "same_password": "A nova senha não pode ser igual à senha atual.",
    }.get(code, "Link inválido. Solicite um novo link de redefinição.")


@main_blueprint.route("/esqueci-senha", methods=["GET", "POST"])
def esqueci_senha():
    from loja.services.password_reset import PUBLIC_MESSAGE, request_password_reset

    if request.method == "POST":
        # Mesma resposta pública em TODOS os casos (conta existente,
        # inexistente, sem senha local ou no limite): flash idêntico + PRG.
        forwarded = request.headers.get("X-Forwarded-For", "")
        ip = forwarded.split(",")[0].strip() or (request.remote_addr or "")
        request_password_reset(
            request.form.get("email", ""),
            ip=ip or None,
        )
        flash(PUBLIC_MESSAGE, "info")
        return redirect(url_for("main.esqueci_senha"))

    return render_template("conta/esqueci_senha.html")


@main_blueprint.route("/redefinir-senha", methods=["GET", "POST"])
def redefinir_senha():
    from loja.services.password_reset import (
        reset_password_with_token,
        validate_reset_token,
    )

    if request.method == "GET":
        token = request.args.get("token", "")
        status = validate_reset_token(token)
        if status == "ok":
            return _headers_pagina_redefinicao(
                render_template("conta/redefinir_senha.html",
                                token=token, error=None)
            )
        return _headers_pagina_redefinicao(
            render_template(
                "conta/redefinir_senha.html",
                token=None,
                error=_mensagem_erro_redefinicao(status),
            )
        )

    token = request.form.get("token", "")
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")

    if not password:
        error = _mensagem_erro_redefinicao("empty")
    elif password != confirm:
        error = "As senhas não coincidem."
    else:
        ok, code = reset_password_with_token(token, password)
        if ok:
            # Sucesso: SEM autenticar — usuário entra na tela de login.
            flash(
                "Senha redefinida com sucesso! Faça login com a nova senha.",
                "success",
            )
            return _headers_pagina_redefinicao(
                redirect(url_for("main.login"))
            )
        error = _mensagem_erro_redefinicao(code)
        # Falha não consome o token: o form volta com ele para nova tentativa.
        token = token if code in ("empty", "same_password") else None

    return _headers_pagina_redefinicao(
        render_template(
            "conta/redefinir_senha.html",
            token=token,
            error=error,
        )
    )


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

            # zip_code é String(10) no banco; CEP mais longo estouraria
            # DataError no commit (caía no handler genérico).
            if len(zip_code) > 10:
                flash('CEP muito longo.', 'danger')
                return render_edit_form(400)

        try:
            current_user.name = new_username
            current_user.email = new_email
            current_user.phone = new_phone or None

            if should_save_address:
                # A relação precisa ser lida ANTES de qualquer Address pendente:
                # cada acesso lazy à relação dispara autoflush e um INSERT com
                # as colunas NOT NULL ainda nulas estouraria IntegrityError
                # (bug do primeiro endereço, que o except genérico reportava
                # como "e-mail já cadastrado").
                current_addresses = list(current_user.addresses)

                if address is None:
                    if not current_addresses:
                        # O primeiro endereço cadastrado sempre vira o principal.
                        make_default = True

                    address = Address(
                        id=uuid.uuid4(),
                        user_id=current_user.id,
                    )
                    db.session.add(address)

                # Preenche todas as colunas NOT NULL antes de qualquer query:
                # o autoflush do bloco abaixo só pode ver um Address completo.
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
                    or len(current_addresses) <= 1
                )

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

            db.session.commit()

            flash('Conta e endereço atualizados com sucesso!', 'success')
            return redirect(url_for('main.conta'))

        except IntegrityError:
            db.session.rollback()
            # Nem toda violação de constraint é e-mail duplicado (era o que o
            # flash antigo afirmava). Após o rollback, refaz o pre-check: a
            # única forma de users.email violar a unicidade é existir outra
            # conta com o mesmo e-mail (corrida contra o check acima).
            email_conflict = (
                User.query
                .filter(
                    User.email == new_email,
                    User.id != current_user.id,
                )
                .first()
            )
            if email_conflict:
                flash(
                    'Não foi possível salvar. Verifique se o e-mail já está cadastrado.',
                    'danger',
                )
            else:
                flash(
                    'Não foi possível salvar os dados. Verifique os campos e tente novamente.',
                    'danger',
                )
            return render_edit_form(409)

        except Exception as error:
            db.session.rollback()
            current_app.logger.error("Erro ao atualizar conta/endereço: %s", error)
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
@main_blueprint.route("/delete_account", methods=["POST"])
@login_required
def delete_account():
    try:
        user = current_user._get_current_object()

        # orders.user_id usa ON DELETE RESTRICT. Além de respeitar a FK,
        # preservar o histórico de pedidos evita apagar dados comerciais que
        # precisam continuar vinculados ao cliente.
        possui_pedidos = (
            Order.query
            .filter(Order.user_id == user.id)
            .first()
            is not None
        )

        if possui_pedidos:
            flash(
                "Sua conta possui pedidos vinculados e não pode ser excluída "
                "diretamente. Entre em contato com o atendimento caso precise "
                "solicitar a desativação da conta.",
                "warning",
            )
            return redirect(url_for("main.conta"))

        db.session.delete(user)
        db.session.commit()

        # Faz logout somente depois do commit. Se o banco rejeitar a operação,
        # o cliente continua autenticado e consegue retornar à própria conta.
        logout_user()

        flash("Conta excluída com sucesso!", "success")
        return redirect(url_for("main.login"))

    except Exception as error:
        db.session.rollback()
        current_app.logger.error("Erro ao excluir conta: %s", error)
        flash(
            "Ocorreu um erro ao tentar excluir a conta. Tente novamente.",
            "danger",
        )
        return redirect(url_for("main.conta"))


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
    supplier_tracking_data = ()

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

        supplier_tracking_data = build_customer_tracking(order)

    return render_template(
        "tracking/acompanhar_pedido.html",
        order=order,
        error=error,
        shipment_data=shipment_data,
        all_statuses=TRACKING_STATUS,
        supplier_tracking_data=supplier_tracking_data,
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
    sitemap_url = url_for("main.sitemap_xml", _external=True)
    linhas = [
        "User-agent: *",
        "Disallow: /conta/",
        "Disallow: /admin/",
        "Disallow: /cart/",
        "Disallow: /checkout/",
        "",
        f"Sitemap: {sitemap_url}",
    ]
    return Response("\n".join(linhas), mimetype="text/plain")


@main_blueprint.route("/sitemap.xml")
def sitemap_xml():
    """Sitemap público (somente produtos ativos + páginas institucionais).

    Read-only e sem dados privados: não lista contas, pedidos nem URLs de
    checkout. Qualquer produto fora de estoque continua indexável e é
    resolvido na página (oferta fora de estoque).
    """
    from loja.services.seo import build_sitemap_urls, build_sitemap_xml

    urls = build_sitemap_urls()
    xml = build_sitemap_xml(urls)
    return Response(xml, mimetype="application/xml")


@main_blueprint.route("/feeds/google-merchant.xml")
def google_merchant_feed():
    """Feed preparado para Google Merchant; hard-off por padrão."""
    from loja.services.google_merchant_feed import (
        build_google_merchant_feed,
        find_google_merchant_products,
        is_google_merchant_feed_enabled,
    )

    if not is_google_merchant_feed_enabled():
        abort(404)

    xml = build_google_merchant_feed(find_google_merchant_products())
    response = Response(xml, mimetype="application/rss+xml")
    response.headers["X-Robots-Tag"] = "noindex"
    return response
