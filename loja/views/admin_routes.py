from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID as PythonUUID, uuid4

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    current_app,
    abort,
)
from flask_login import login_required, logout_user, current_user
from werkzeug.security import generate_password_hash
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from loja.utils.decorators import admin_required
from loja.utils import as_sao_paulo
from loja.modules.footer_links import is_safe_footer_url
from loja.modules.services import get_layout_data, invalidate_footer_cache

_FOOTER_URL_ERROR = (
    "URL inválida para link do rodapé. "
    "Use um caminho interno funcional (ex.: /sobre) ou https://."
)


def _footer_url_ok(url: str) -> bool:
    """Validação compartilhada dos 4 grupos de links do rodapé."""
    return is_safe_footer_url(url)

from loja.modules.models import (
    db,
    Product, User,
    LegalLink, CategoryLink, NavLink,
    ContactInfo, FooterConfig, SocialLink,
    SiteConfig, HeroSlide, Testimonial,
    Supplier, Promotion, Coupon,
)
from loja.services.publication import PublicationBlocked, publish_product
from loja.services.publication_unit_economics_dashboard import (
    build_unit_economics_dashboard,
    dashboard_totals,
)
from loja.services.unit_economics_policy import load_unit_economics_cost_policy

admin_blueprint = Blueprint(
    "admin",
    __name__,
    template_folder="templates/admin"
)

ORDER_STATUS_LABELS = {
    "pending_payment": "Aguardando pagamento", "paid": "Pago",
    "manual_review": "Revisão manual", "payment_failed": "Pagamento recusado",
    "expired": "Expirado", "cancelled": "Cancelado",
    "refunded": "Reembolsado", "charged_back": "Contestação",
}


@admin_blueprint.route("/admin/pedidos")
@admin_required
def orders_list():
    """Consulta administrativa sem sincronizar ou alterar pagamentos."""
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.services.order_payment_evidence import payment_is_confirmed

    status = request.args.get("status", "")
    if status and status not in ORDER_STATUS_LABELS:
        abort(400)
    search = request.args.get("q", "").strip()[:100]
    query = Order.query.options(selectinload(Order.payments))
    if status:
        query = query.filter(Order.status == status)
    if search:
        # Exact references prevent accidental wildcard searches across clients.
        predicates = [Order.order_number == search, Order.mp_payment_id == search]
        try:
            predicates.append(Order.id == PythonUUID(search))
        except ValueError:
            pass
        query = query.filter(db.or_(*predicates))
    page = query.order_by(Order.created_at.desc(), Order.id.desc()).paginate(
        page=max(1, request.args.get("page", 1, type=int)), per_page=25,
        error_out=False,
    )
    return render_template(
        "admin/orders.html", page=page, status=status, search=search,
        status_labels=ORDER_STATUS_LABELS, payment_is_confirmed=payment_is_confirmed,
    )


@admin_blueprint.route("/admin/pedidos/<uuid:order_id>")
@admin_required
def order_detail(order_id):
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.services.order_payment_evidence import payment_is_confirmed

    order = Order.query.options(
        selectinload(Order.items), selectinload(Order.payments),
        selectinload(Order.shipments),
    ).filter_by(id=order_id).first_or_404()
    from loja.modules.models import (
        ManualOperationalAudit, ManualSupplierPurchase, ShipmentItem,
    )
    purchases = (
        ManualSupplierPurchase.query
        .filter_by(order_id=order.id)
        .order_by(ManualSupplierPurchase.created_at.asc()).all()
    )
    purchase_ids = [p.id for p in purchases]
    coverage = {}
    if purchase_ids:
        from loja.modules.models import Shipment as ShipmentModel

        rows = (
            db.session.query(
                ShipmentItem.order_item_id,
                db.func.coalesce(db.func.sum(ShipmentItem.quantity), 0),
            )
            .join(ShipmentModel, ShipmentModel.id == ShipmentItem.shipment_id)
            .filter(ShipmentModel.manual_purchase_id.in_(purchase_ids))
            .group_by(ShipmentItem.order_item_id)
            .all()
        )
        coverage = {item_id: int(total or 0) for item_id, total in rows}
    known_ids = {str(p.id) for p in purchases} | {
        str(s.id) for s in order.shipments
    }
    audit = [
        a for a in (
            ManualOperationalAudit.query
            .order_by(ManualOperationalAudit.created_at.desc()).limit(100).all()
        )
        if str(a.entity_id) in known_ids
    ]
    return render_template(
        "admin/order_detail.html", order=order, status_labels=ORDER_STATUS_LABELS,
        payment_confirmed=payment_is_confirmed(order),
        manual_purchases=purchases, shipment_coverage=coverage,
        operational_audit=audit,
    )


def _manual_items_from_form():
    """Itens `item_<uuid>=qtd` do formulário (ids persistidos, nunca nomes)."""
    items = {}
    for key, raw in request.form.items():
        if not key.startswith("item_"):
            continue
        try:
            item_id = PythonUUID(key[5:])
        except ValueError:
            raise ValueError("order_item_invalid")
        items[item_id] = raw
    return items


def _manual_back(order_id):
    return redirect(url_for("admin.order_detail", order_id=order_id))


_MANUAL_ERROR_MESSAGES = {
    "order_invalid": "Pedido inválido.",
    "order_not_found": "Pedido não encontrado.",
    "order_status_not_eligible": "Pedido inelegível: exige pagamento comprovado e estado pago.",
    "payment_not_confirmed": "Pagamento não comprovado para este pedido.",
    "order_without_items": "Pedido sem itens para vincular.",
    "order_item_invalid": "Item inválido.",
    "order_item_not_in_order": "Item não pertence a este pedido.",
    "order_item_not_in_purchase": "Item não vinculado a esta compra.",
    "items_required": "Informe ao menos um item com quantidade.",
    "quantity_invalid": "Quantidade inválida (inteiro maior que zero).",
    "quantity_exceeds_sold": "Quantidade registrada ultrapassa a vendida.",
    "quantity_exceeds_purchase": "Quantidade excede a vinculada à compra.",
    "external_order_id_invalid": "Número do pedido no AliExpress inválido.",
    "external_order_id_in_use": "Número externo já registrado em outro pedido.",
    "tracking_code_invalid": "Código de rastreio inválido.",
    "tracking_code_in_use": "Código de rastreio já ligado a outro pedido.",
    "carrier_invalid": "Transportadora inválida.",
    "evidence_ref_invalid": "Referência da evidência inválida.",
    "purchased_at_invalid": "Data/hora da compra inválida.",
    "purchased_tz_invalid": "Fuso horário inválido.",
    "posted_at_invalid": "Data de postagem inválida.",
    "estimated_delivery_invalid": "Previsão de entrega inválida.",
    "expected_updated_at_invalid": "Marcador de versão inválido.",
    "purchase_invalid": "Compra inválida.",
    "purchase_not_found": "Compra não encontrada.",
    "purchase_not_in_order": "Compra não pertence a este pedido.",
    "shipment_invalid": "Remessa inválida.",
    "shipment_not_found": "Remessa não encontrada.",
    "reason_invalid": "Justificativa obrigatória.",
    "stale_state_reload": "Registro alterado por outro operador — recarregue e confira antes de corrigir.",
}


def _manual_error_message(exc):
    return _MANUAL_ERROR_MESSAGES.get(
        str(exc),
        "Dados inválidos; nada foi alterado.",
    )


@admin_blueprint.route(
    "/admin/pedidos/<uuid:order_id>/compra-manual", methods=["POST"]
)
@admin_required
def register_manual_purchase(order_id):
    """Registra compra externa já realizada (nunca compra no fornecedor)."""
    from sqlalchemy.exc import IntegrityError

    from loja.services import manual_dispatch as dispatch

    try:
        result = dispatch.register_manual_purchase(
            order_id=order_id,
            external_order_id=request.form.get("external_order_id"),
            items=_manual_items_from_form(),
            purchased_at=request.form.get("purchased_at"),
            purchased_tz=request.form.get("purchased_tz"),
            evidence_ref=request.form.get("evidence_ref"),
            note=request.form.get("note"),
            admin_user_id=current_user.id,
        )
        db.session.commit()
    except IntegrityError:
        # Corrida: outro registro venceu; retorna o existente sem duplicar.
        db.session.rollback()
        from loja.modules.models import ManualSupplierPurchase

        code = str(request.form.get("external_order_id") or "").strip().upper()
        existing = ManualSupplierPurchase.query.filter_by(
            external_order_id=code
        ).first()
        if existing is not None and str(existing.order_id) == str(order_id):
            flash("Compra já registrada anteriormente.", "warning")
            return _manual_back(order_id)
        current_app.logger.warning("compra manual: conflito de número externo")
        flash("Número externo já registrado em outro pedido.", "danger")
        return _manual_back(order_id)
    except ValueError as exc:
        db.session.rollback()
        current_app.logger.warning("compra manual: %s", exc)
        flash(_manual_error_message(exc), "danger")
        return _manual_back(order_id)
    flash(
        "Compra registrada com sucesso."
        if result.created else "Compra já registrada anteriormente.",
        "success" if result.created else "warning",
    )
    return _manual_back(order_id)


@admin_blueprint.route(
    "/admin/pedidos/compras/<uuid:purchase_id>/corrigir", methods=["POST"]
)
@admin_required
def correct_manual_purchase(purchase_id):
    """Corrige o número externo (auditoria, sem efeitos operacionais)."""
    from loja.modules.models import ManualSupplierPurchase
    from loja.services import manual_dispatch as dispatch

    purchase = ManualSupplierPurchase.query.filter_by(id=purchase_id).first()
    if purchase is None:
        flash("Compra não encontrada.", "danger")
        return redirect(url_for("admin.dashboard"))
    try:
        purchase = dispatch.correct_manual_purchase(
            purchase_id=purchase_id,
            external_order_id=request.form.get("external_order_id"),
            reason=request.form.get("reason"),
            admin_user_id=current_user.id,
            expected_updated_at=request.form.get("expected_updated_at"),
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        current_app.logger.warning("correção de compra: %s", exc)
        flash(_manual_error_message(exc), "danger")
        return _manual_back(purchase.order_id)
    flash("Número externo corrigido com auditoria.", "success")
    return _manual_back(purchase.order_id)


@admin_blueprint.route(
    "/admin/pedidos/compras/<uuid:purchase_id>/rastreio", methods=["POST"]
)
@admin_required
def register_manual_shipment(purchase_id):
    """Registra remessa vinculada à compra (nunca posta/entrega)."""
    from sqlalchemy.exc import IntegrityError

    from loja.modules.models import ManualSupplierPurchase
    from loja.services import manual_dispatch as dispatch

    purchase = ManualSupplierPurchase.query.filter_by(id=purchase_id).first()
    if purchase is None:
        flash("Compra não encontrada.", "danger")
        return redirect(url_for("admin.dashboard"))
    try:
        result = dispatch.register_manual_shipment(
            order_id=purchase.order_id,
            purchase_id=purchase.id,
            tracking_code=request.form.get("tracking_code"),
            carrier=request.form.get("carrier"),
            posted_at=request.form.get("posted_at") or None,
            estimated_delivery=request.form.get("estimated_delivery") or None,
            evidence_ref=request.form.get("evidence_ref"),
            items=_manual_items_from_form(),
            admin_user_id=current_user.id,
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        from loja.modules.models import Shipment

        code = str(request.form.get("tracking_code") or "").strip().upper()
        existing = Shipment.query.filter_by(tracking_code=code).first()
        if existing is not None and str(existing.order_id) == str(purchase.order_id):
            flash("Rastreio já registrado anteriormente.", "warning")
            return _manual_back(purchase.order_id)
        current_app.logger.warning("rastreio manual: código em outro pedido")
        flash("Código de rastreio já ligado a outro pedido.", "danger")
        return _manual_back(purchase.order_id)
    except ValueError as exc:
        db.session.rollback()
        current_app.logger.warning("rastreio manual: %s", exc)
        flash(_manual_error_message(exc), "danger")
        return _manual_back(purchase.order_id)
    flash(
        "Rastreio registrado (aguardando postagem)."
        if result.created else "Rastreio já registrado anteriormente.",
        "success" if result.created else "warning",
    )
    return _manual_back(purchase.order_id)


@admin_blueprint.route(
    "/admin/pedidos/remessas/<int:shipment_id>/corrigir", methods=["POST"]
)
@admin_required
def correct_manual_shipment(shipment_id):
    """Corrige transportadora/rastreio (auditoria, sem efeitos)."""
    from loja.modules.models import Shipment
    from loja.services import manual_dispatch as dispatch

    shipment = Shipment.query.filter_by(id=shipment_id).first()
    if shipment is None:
        flash("Remessa não encontrada.", "danger")
        return redirect(url_for("admin.dashboard"))
    try:
        dispatch.correct_manual_shipment(
            shipment_id=shipment.id,
            carrier=request.form.get("carrier"),
            tracking_code=request.form.get("tracking_code") or None,
            reason=request.form.get("reason"),
            admin_user_id=current_user.id,
            expected_updated_at=request.form.get("expected_updated_at"),
        )
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        current_app.logger.warning("correção de rastreio: %s", exc)
        flash(_manual_error_message(exc), "danger")
        return _manual_back(shipment.order_id)
    flash("Remessa corrigida com auditoria.", "success")
    return _manual_back(shipment.order_id)

# ── Variáveis de contexto globais ────────────────────────────────────────────────────────

@admin_blueprint.context_processor
def inject_global_data():
    return get_layout_data()


def _normalize_coupon_code(value):
    """Normaliza o código para comparação e gravação."""
    return "".join((value or "").strip().upper().split())


def _parse_decimal_field(
    field_name,
    *,
    required=False,
    default=None,
    minimum=None,
):
    raw_value = (request.form.get(field_name) or "").strip().replace(",", ".")

    if not raw_value:
        if required:
            raise ValueError(f"O campo {field_name} é obrigatório.")
        return default

    try:
        value = Decimal(raw_value)
    except InvalidOperation as error:
        raise ValueError(f"O valor informado em {field_name} é inválido.") from error

    if minimum is not None and value < minimum:
        raise ValueError(
            f"O campo {field_name} deve ser maior ou igual a {minimum}."
        )

    return value.quantize(Decimal("0.01"))


def _parse_optional_integer(field_name):
    raw_value = (request.form.get(field_name) or "").strip()

    if not raw_value:
        return None

    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"O campo {field_name} deve ser um número inteiro.") from error

    if value < 1:
        raise ValueError(f"O campo {field_name} deve ser maior ou igual a 1.")

    return value


def _parse_optional_datetime(field_name):
    raw_value = (request.form.get(field_name) or "").strip()

    if not raw_value:
        return None

    try:
        return datetime.fromisoformat(raw_value)
    except ValueError as error:
        raise ValueError(
            f"A data informada em {field_name} é inválida."
        ) from error



def _promotion_form_data():
    """Valida e normaliza os dados enviados pelo formulário de promoção."""
    product_id_raw = (request.form.get("product_id") or "").strip()

    if not product_id_raw:
        raise ValueError("Selecione um produto para a promoção.")

    try:
        product_id = PythonUUID(product_id_raw)
    except (ValueError, TypeError) as error:
        raise ValueError("O produto selecionado é inválido.") from error

    product = Product.query.get(product_id)

    if product is None:
        raise ValueError("O produto selecionado não existe.")

    sale_price = _parse_decimal_field(
        "sale_price",
        required=True,
        minimum=Decimal("0.01"),
    )

    original_price = Decimal(str(product.price))

    if sale_price >= original_price:
        raise ValueError(
            "O preço promocional deve ser menor que o preço original "
            f"do produto (R$ {original_price:.2f})."
        )

    starts_at = _parse_optional_datetime("starts_at")
    ends_at = _parse_optional_datetime("ends_at")

    if starts_at and ends_at and ends_at <= starts_at:
        raise ValueError(
            "A data final da promoção deve ser posterior à data inicial."
        )

    is_active = request.form.get("is_active") in {
        "1",
        "on",
        "true",
        "True",
    }

    return {
        "product": product,
        "sale_price": sale_price,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "is_active": is_active,
    }

def _coupon_form_data(*, current_coupon=None):
    code = _normalize_coupon_code(request.form.get("code"))
    description = (request.form.get("description") or "").strip() or None
    discount_type = (request.form.get("discount_type") or "").strip()

    if not code:
        raise ValueError("Informe o código do cupom.")

    if len(code) > 50:
        raise ValueError("O código do cupom deve possuir no máximo 50 caracteres.")

    if description and len(description) > 255:
        raise ValueError("A descrição deve possuir no máximo 255 caracteres.")

    if discount_type not in {"percentage", "fixed"}:
        raise ValueError("Selecione um tipo de desconto válido.")

    discount_value = _parse_decimal_field(
        "discount_value",
        required=True,
        minimum=Decimal("0.01"),
    )

    if discount_type == "percentage" and discount_value > Decimal("100"):
        raise ValueError("O desconto percentual não pode ser maior que 100%.")

    minimum_order_amount = _parse_decimal_field(
        "minimum_order_amount",
        default=Decimal("0.00"),
        minimum=Decimal("0.00"),
    )

    maximum_discount_amount = _parse_decimal_field(
        "maximum_discount_amount",
        default=None,
        minimum=Decimal("0.00"),
    )

    # O teto só é utilizado em cupons percentuais.
    if discount_type == "fixed":
        maximum_discount_amount = None

    starts_at = _parse_optional_datetime("starts_at")
    ends_at = _parse_optional_datetime("ends_at")

    if starts_at and ends_at and ends_at <= starts_at:
        raise ValueError("A data final deve ser posterior à data inicial.")

    usage_limit = _parse_optional_integer("usage_limit")

    if (
        current_coupon is not None
        and usage_limit is not None
        and usage_limit < current_coupon.times_used
    ):
        raise ValueError(
            "O limite de usos não pode ser menor que a quantidade já utilizada."
        )

    existing_query = Coupon.query.filter(
        db.func.upper(Coupon.code) == code
    )

    if current_coupon is not None:
        existing_query = existing_query.filter(
            Coupon.id != current_coupon.id
        )

    if existing_query.first():
        raise ValueError("Já existe um cupom com este código.")

    return {
        "code": code,
        "description": description,
        "discount_type": discount_type,
        "discount_value": discount_value,
        "minimum_order_amount": minimum_order_amount,
        "maximum_discount_amount": maximum_discount_amount,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "usage_limit": usage_limit,
        "first_purchase_only": (
            request.form.get("first_purchase_only") == "1"
        ),
        "is_active": request.form.get("is_active") == "1",
    }

# -------------------------------------------------------------------
# Dashboard
# -------------------------------------------------------------------

@admin_blueprint.route("/admin", endpoint="dashboard")
@admin_blueprint.route("/admin")
@admin_required
def admin_dashboard():

    if not current_user.is_admin:
        flash("Acesso negado.", "danger")
        return redirect(url_for("main.home"))

    # O dashboard assume que os singletons existem. Em primeiro acesso,
    # cria os registros com valores padrão para não quebrar o template.
    if SiteConfig.query.first() is None:
        db.session.add(SiteConfig(id=uuid4(), name="Pollo Modas"))
    if FooterConfig.query.first() is None:
        db.session.add(FooterConfig(id=uuid4(), brand_name="Pollo Modas"))
    if ContactInfo.query.first() is None:
        db.session.add(
            ContactInfo(id=uuid4(), email="contato@pollomodas.com.br")
        )
    db.session.commit()

    # Reaproveita os dados do context processor (evita re-consultar as
    # mesmas tabelas) e carrega eageramente os relacionamentos usados no
    # template, para não gerar N+1 queries por linha da tabela.
    layout = get_layout_data()
    footer = layout["footer"]

    products = Product.query.options(selectinload(Product.suppliers)).all()
    promotions = (
        Promotion.query
        .options(selectinload(Promotion.product))
        .order_by(Promotion.created_at.desc())
        .all()
    )
    suppliers = Supplier.query.options(selectinload(Supplier.products)).all()
    users = User.query.all()
    coupons = Coupon.query.order_by(Coupon.created_at.desc()).all()

    return render_template(
        "admin/dashboard.html",
        products=products,
        users=users,
        promotions=promotions,
        coupons=coupons,
        suppliers=suppliers,
        testimonials=layout["testimonials"],
        hero_slides=layout["hero_slides"],
        config=layout["site"],
        social_links=footer["social_links"],
        footer_config=FooterConfig.query.first(),
        contact_info=footer["contact"],
        nav_links=footer["nav_links"],
        category_links=footer["categories"],
        legal_links=footer["legal_links"],
    )


# -------------------------------------------------------------------
# Dashboard de unit economics (FASE 8.2.0, somente leitura)
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/unit-economics", methods=["GET"])
@admin_required
def unit_economics_dashboard():

    products = Product.query.options(
        selectinload(Product.supplier_links)
    ).all()
    rows = build_unit_economics_dashboard(
        products,
        policy=load_unit_economics_cost_policy(),
    )
    totals = dashboard_totals(rows)

    return render_template(
        "admin/unit_economics_dashboard.html",
        rows=rows,
        totals=totals,
    )


# -------------------------------------------------------------------
# Dashboard de métricas por canal (FASE 9.0.0-B, somente leitura)
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/metricas-por-canal", methods=["GET"])
@admin_required
def channel_metrics_dashboard():
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.services.acquisition_sessions import (
        first_session_day,
        session_counts_by_channel,
    )
    from loja.services.channel_metrics import build_channel_metrics

    session_counts = session_counts_by_channel()
    measurement_start = first_session_day()
    query = Order.query.options(
        selectinload(Order.items),
        selectinload(Order.payments),
    )
    if measurement_start is not None:
        query = query.filter(
            Order.created_at >= datetime.combine(
                measurement_start,
                datetime.min.time(),
            )
        )
    rows, totals = build_channel_metrics(
        query.all(),
        policy=load_unit_economics_cost_policy(),
        session_counts=session_counts,
    )

    return render_template(
        "admin/channel_metrics_dashboard.html",
        rows=rows,
        totals=totals,
        measurement_start=measurement_start,
    )


@admin_blueprint.route(
    "/admin/prontidao-vendas",
    methods=["GET"],
)
@admin_required
def sales_readiness():
    """Painel somente leitura de prontidão para a primeira venda real.

    Apenas GET, sem commit, sem chamadas a provedores e sem formulários
    de mutação. PRONTO significa pronto para receber venda — nunca
    autorização para compra automática no fornecedor.
    """
    from loja.services.sales_readiness import (
        REASON_LABELS,
        evaluate_sales_readiness,
    )

    summary = evaluate_sales_readiness()
    return render_template(
        "admin/sales_readiness.html",
        summary=summary,
        reason_labels=REASON_LABELS,
    )


@admin_blueprint.route(
    "/admin/prontidao-vendas/cotar/<uuid:supplier_product_id>",
    methods=["POST"],
)
@admin_required
def request_sales_readiness_quote(supplier_product_id):
    """Boundary individual, gateada e read-only no fornecedor.

    Sem gate ou provider revisado, falha fechado e não grava. CSRF global e
    admin_required protegem o POST. A função não importa compra/fulfillment.
    """
    if not current_app.config.get("ALIEXPRESS_REFERENCE_QUOTE_ENABLED", False):
        flash("Cotação de referência desabilitada.", "warning")
        return redirect(url_for("admin.sales_readiness"))

    from loja.services.sales_readiness import load_reference_scenario
    from loja.services.supplier_freight_quotes import (
        ReferenceQuoteUnavailable,
        request_reference_freight_quote,
    )

    from loja.modules.models import Supplier, SupplierProduct

    link = SupplierProduct.query.filter_by(
        id=supplier_product_id, is_active=True
    ).first()
    supplier = (
        Supplier.query.filter_by(id=link.supplier_id).first()
        if link is not None and link.supplier_id is not None
        else None
    )
    if (
        link is None
        or supplier is None
        or supplier.channel_type != "aliexpress"
        or not supplier.seller_id
    ):
        flash("Vínculo de fornecedor indisponível.", "warning")
        return redirect(url_for("admin.sales_readiness"))

    dest_cep, option = load_reference_scenario()
    provider = current_app.config.get("ALIEXPRESS_REFERENCE_QUOTE_PROVIDER")
    if not dest_cep or not callable(provider):
        flash("Cotação de referência indisponível.", "warning")
        return redirect(url_for("admin.sales_readiness"))

    try:
        request_reference_freight_quote(
            supplier_product_id=supplier_product_id,
            dest_cep=dest_cep,
            option=option,
            qty=1,
            provider=provider,
        )
    except ReferenceQuoteUnavailable:
        flash("Não foi possível comprovar o frete de referência.", "warning")
    else:
        flash("Cotação de referência atualizada.", "success")
    return redirect(url_for("admin.sales_readiness"))


@admin_blueprint.route(
    "/admin/recuperacao-checkout",
    methods=["GET", "POST"],
)
@admin_required
def checkout_recovery():
    """Fila read-only e lote opcional, protegido por gate próprio."""
    from loja.modules.enviar_email import email_delivery_ready
    from loja.services.checkout_recovery import (
        find_recoverable_orders,
        is_batch_recovery_enabled,
        is_recovery_enabled,
        load_after_hours,
        run_checkout_recovery,
    )
    from loja.services.stock_reservations import (
        count_unproven_stock_reservations,
        find_active_stock_reservations,
        find_expired_stock_reservations,
        is_scheduled_release_enabled,
        load_reservation_hours,
    )

    enabled = is_recovery_enabled()
    batch_enabled = is_batch_recovery_enabled()
    email_ready = email_delivery_ready()
    result = None

    if request.method == "POST":
        try:
            result = run_checkout_recovery()
        except ValueError:
            flash(
                "Entrega de e-mail indisponível; nenhum lembrete foi enviado.",
                "danger",
            )

    try:
        after_hours = load_after_hours()
    except ValueError:
        after_hours = None

    try:
        candidates = find_recoverable_orders(after_hours=after_hours, limit=200)
    except ValueError:
        candidates = []

    try:
        reservation_hours = load_reservation_hours()
        expired_reservations = find_expired_stock_reservations(
            hours=reservation_hours,
            limit=200,
        )
        active_reservations = [
            {
                "order_number": order.order_number,
                "created_sp": _format_sp(order.created_at),
                "expires_sp": _format_sp(order.mp_preference_expires_at),
            }
            for order in find_active_stock_reservations(limit=200)
        ]
        unproven_reservations_count = count_unproven_stock_reservations()
    except ValueError:
        reservation_hours = None
        expired_reservations = []
        active_reservations = []
        unproven_reservations_count = 0

    return render_template(
        "admin/checkout_recovery.html",
        enabled=enabled,
        batch_enabled=batch_enabled,
        email_ready=email_ready,
        after_hours=after_hours,
        candidates=candidates,
        result=result,
        reservation_release_enabled=is_scheduled_release_enabled(),
        reservation_batch_release_enabled=bool(
            current_app.config.get(
                "STOCK_RESERVATION_BATCH_RELEASE_ENABLED",
                False,
            )
        ),
        reservation_hours=reservation_hours,
        expired_reservations=expired_reservations,
        active_reservations=active_reservations,
        unproven_reservations_count=unproven_reservations_count,
    )


def _format_sp(value):
    """Formata datetime (naive UTC do banco) em America/Sao_Paulo.

    Retorna "dd/mm/aaaa às HH:MM" ou "—" quando ausente.
    """
    local = as_sao_paulo(value)
    if local is None:
        return "—"
    return local.strftime("%d/%m/%Y às %H:%M")


@admin_blueprint.route(
    "/admin/recuperacao-checkout/<uuid:order_id>/enviar",
    methods=["POST"],
)
@admin_required
def send_checkout_recovery(order_id):
    """Envia somente o candidato escolhido, com revalidação sob lock."""
    from loja.services.checkout_recovery import send_single_checkout_recovery

    try:
        result = send_single_checkout_recovery(order_id)
    except ValueError:
        flash(
            "Entrega de e-mail indisponível; nenhum lembrete foi enviado.",
            "danger",
        )
        return redirect(url_for("admin.checkout_recovery"))

    if not result.enabled:
        flash("Envio individual está desligado.", "warning")
    elif result.sent == 1:
        flash("Lembrete individual enviado com sucesso.", "success")
    elif result.failed:
        flash("Falha sanitizada no envio; o pedido continua pendente.", "danger")
    else:
        flash("Pedido não elegível ou já processado; nada foi enviado.", "warning")
    return redirect(url_for("admin.checkout_recovery"))


@admin_blueprint.route(
    "/admin/recuperacao-checkout/<uuid:order_id>/ignorar",
    methods=["POST"],
)
@admin_required
def skip_checkout_recovery(order_id):
    """Descarta somente o lembrete escolhido; não toca pagamento ou estoque."""
    if request.form.get("confirm_skip") != "1":
        flash("Confirme o descarte do lembrete antes de continuar.", "warning")
        return redirect(url_for("admin.checkout_recovery"))

    from loja.services.checkout_recovery import skip_single_checkout_recovery

    skipped = skip_single_checkout_recovery(
        order_id,
        admin_user_id=current_user.id,
    )
    if skipped:
        flash(
            "Candidato removido da fila de lembretes. "
            "Pedido, pagamento e reserva não foram alterados.",
            "success",
        )
    else:
        flash("Pedido não elegível ou já processado; nada foi alterado.", "warning")
    return redirect(url_for("admin.checkout_recovery"))


@admin_blueprint.route(
    "/admin/reservas-expiradas/<uuid:order_id>/liberar",
    methods=["POST"],
)
@admin_required
def release_single_expired_reservation(order_id):
    """Libera apenas a reserva escolhida, após confirmação e revalidação."""
    if request.form.get("confirm_release") != "1":
        flash("Confirme a liberação da reserva antes de continuar.", "warning")
        return redirect(url_for("admin.checkout_recovery"))

    from loja.services.stock_reservations import (
        release_single_expired_stock_reservation,
    )

    released = release_single_expired_stock_reservation(
        order_id,
        source="admin_single",
    )
    if released:
        flash("Reserva vencida liberada com sucesso.", "success")
    else:
        flash(
            "Pedido não elegível, não vencido ou com pagamento aprovado; "
            "nenhum estoque foi alterado.",
            "warning",
        )
    return redirect(url_for("admin.checkout_recovery"))


@admin_blueprint.route(
    "/admin/reservas-expiradas/liberar",
    methods=["POST"],
)
@admin_required
def release_expired_reservations():
    """Lote administrativo separado e fail-closed."""
    if not current_app.config.get(
        "STOCK_RESERVATION_BATCH_RELEASE_ENABLED",
        False,
    ):
        flash("Liberação em lote está desligada.", "warning")
        return redirect(url_for("admin.checkout_recovery"))

    from loja.services.stock_reservations import (
        release_expired_stock_reservations,
    )

    try:
        result = release_expired_stock_reservations(source="admin_batch")
    except ValueError:
        flash(
            "Configuração da janela de reserva inválida; nenhuma reserva foi alterada.",
            "danger",
        )
        return redirect(url_for("admin.checkout_recovery"))

    flash(
        (
            f"Reservas vencidas processadas: {result.released} liberada(s), "
            f"{result.skipped} ignorada(s)."
        ),
        "success",
    )
    return redirect(url_for("admin.checkout_recovery"))


# -------------------------------------------------------------------
# Item 1.3: rota de saída de manual_review (aprovar / rejeitar)
# -------------------------------------------------------------------

def _flash_manual_review_result(result):
    """Converte ManualReviewResult em flash legível (motivos sanitizados)."""
    if result.outcome == "changed":
        if result.action == "approve":
            flash(
                "Pedido aprovado: estoque revalidado e pedido marcado como pago.",
                "success",
            )
        else:
            flash(
                "Pedido recusado (cancelled); a reserva, se existia, foi "
                "devolvida ao estoque.",
                "success",
            )
    elif result.outcome == "noop":
        flash("Pedido não está em revisão manual; nada foi alterado.", "warning")
    elif result.reasons == ("reason_required",):
        flash("Informe o motivo da rejeição.", "warning")
    else:
        flash(
            "Bloqueado sem alterações: " + " ".join(result.reasons),
            "warning",
        )


@admin_blueprint.route("/admin/pedidos/revisao")
@admin_required
def manual_review_list():
    """Fila dos pedidos travados em manual_review (item 1.3)."""
    from loja.modules.getway_mercado_pago.payment_models import Order

    orders = (
        Order.query
        .filter_by(status="manual_review")
        .order_by(Order.created_at.asc(), Order.id.asc())
        .all()
    )
    return render_template("admin/manual_review.html", orders=orders)


@admin_blueprint.route(
    "/admin/pedidos/revisao/<uuid:order_id>/aprovar",
    methods=["POST"],
)
@admin_required
def manual_review_approve(order_id):
    """Aprovação: revalidação de estoque sob lock + promoção a paid."""
    if request.form.get("confirm_approve") != "1":
        flash("Confirme a aprovação do pedido antes de continuar.", "warning")
        return redirect(url_for("admin.manual_review_list"))

    from loja.services.order_promotion import approve_manual_review

    result = approve_manual_review(
        order_id,
        admin_user_id=current_user.id,
        note=request.form.get("note"),
    )
    _flash_manual_review_result(result)
    return redirect(url_for("admin.manual_review_list"))


@admin_blueprint.route(
    "/admin/pedidos/revisao/<uuid:order_id>/rejeitar",
    methods=["POST"],
)
@admin_required
def manual_review_reject(order_id):
    """Rejeição: devolve a reserva e marca cancelled, com motivo auditável."""
    reason = str(request.form.get("reason") or "").strip()
    if not reason:
        flash("Informe o motivo da rejeição.", "warning")
        return redirect(url_for("admin.manual_review_list"))
    if request.form.get("confirm_reject") != "1":
        flash("Confirme a rejeição do pedido antes de continuar.", "warning")
        return redirect(url_for("admin.manual_review_list"))

    from loja.services.order_promotion import reject_manual_review

    try:
        result = reject_manual_review(
            order_id,
            admin_user_id=current_user.id,
            reason=reason,
        )
    except ValueError:
        flash(
            "Falha de integridade ao devolver o estoque; nada foi alterado.",
            "danger",
        )
        return redirect(url_for("admin.manual_review_list"))

    _flash_manual_review_result(result)
    return redirect(url_for("admin.manual_review_list"))


# -------------------------------------------------------------------
# Adicionar produto
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/products/add", methods=["GET", "POST"])
@admin_required
def add_product():

    if request.method == "POST":

        name        = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        price       = request.form.get("price", "").strip()
        image1      = request.form.get("image1", "").strip()
        image2      = request.form.get("image2", "").strip()
        image3      = request.form.get("image3", "").strip()
        image4      = request.form.get("image4", "").strip()
        category    = request.form.get("category", "").strip()
        stock       = request.form.get("stock", "0").strip()

        try:
            product = Product(
                name        = name,
                description = description,
                price       = float(price),
                image1      = image1 or None,
                image2      = image2 or None,
                image3      = image3 or None,
                image4      = image4 or None,
                category    = category or None,
                stock       = int(stock),
                is_active   = True,
            )
            db.session.add(product)
            db.session.commit()
            flash("Produto adicionado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="products"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/products/add_product.html")


# -------------------------------------------------------------------
# Editar produto
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/products/edit/<uuid:id>", methods=["GET", "POST"])
@admin_required
def edit_product(id):

    product = Product.query.get_or_404(id)

    if request.method == "POST":

        product.name        = request.form.get("name")
        product.description = request.form.get("description")
        product.price       = float(request.form.get("price"))
        product.image1      = request.form.get("image1") or None
        product.image2      = request.form.get("image2") or None
        product.image3      = request.form.get("image3") or None
        product.image4      = request.form.get("image4") or None
        product.category    = request.form.get("category") or None
        product.stock       = int(request.form.get("stock"))
        desired_active      = request.form.get("is_active") == "on"

        try:
            if product.source == "aliexpress":
                # Todo caminho de PUBLICAÇÃO de AliExpress passa por
                # publish_product(). Ativar bloqueado => flash amigável com
                # códigos sanitizados e NENHUMA alteração dos flags (rollback).
                if desired_active:
                    publish_product(product)
                else:
                    product.is_active = False
            else:
                product.is_active = desired_active
            db.session.commit()
            flash("Produto atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="products"))

        except PublicationBlocked as exc:
            db.session.rollback()
            flash(
                "Não foi possível publicar o produto AliExpress (motivos: "
                + ", ".join(exc.reasons) + ").",
                "danger",
            )

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/products/edit_product.html", product=product)


# -------------------------------------------------------------------
# Excluir produto
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/products/delete/<uuid:id>", methods=["POST"])
@admin_required
def delete_product(id):

    product = Product.query.get_or_404(id)
    db.session.delete(product)
    db.session.commit()
    flash("Produto excluído com sucesso!", "success")
    return redirect(url_for("admin.admin_dashboard", _anchor="products"))


# -------------------------------------------------------------------
# Adicionar promoção
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_promotion", methods=["GET", "POST"])
@admin_required
def add_promotion():

    # Apenas produtos ativos ficam disponíveis para novas promoções.
    products = (
        Product.query
        .filter(Product.is_active.is_(True))
        .order_by(Product.name.asc())
        .all()
    )

    if request.method == "POST":

        try:
            data = _promotion_form_data()

            promo = Promotion(
                product_id=data["product"].id,
                sale_price=data["sale_price"],
                starts_at=data["starts_at"],
                ends_at=data["ends_at"],
                is_active=data["is_active"],
            )

            db.session.add(promo)
            db.session.commit()

            flash(
                f'Promoção para "{data["product"].name}" adicionada com sucesso!',
                "success"
            )

            return redirect(
                url_for(
                    "admin.admin_dashboard",
                    _anchor="promotions"
                )
            )

        except ValueError as error:
            db.session.rollback()
            flash(str(error), "danger")

        except IntegrityError:
            db.session.rollback()
            flash(
                "Não foi possível criar a promoção. "
                "Verifique os dados informados.",
                "danger"
            )

        except Exception as error:
            db.session.rollback()
            current_app.logger.error("Erro ao criar promoção: %s", error)
            flash(
                "Ocorreu um erro ao criar a promoção.",
                "danger"
            )

    return render_template(
        "admin/promotions/add_promotions.html",
        products=products,
    )


# -------------------------------------------------------------------
# Editar promoção
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/edit_promotion/<uuid:id>", methods=["GET", "POST"])
@admin_required
def edit_promotion(id):

    promo = Promotion.query.get_or_404(id)

    # Na edição todos os produtos são exibidos para que uma promoção existente
    # continue editável mesmo se o produto tiver sido desativado depois.
    products = (
        Product.query
        .order_by(Product.name.asc())
        .all()
    )

    if request.method == "POST":

        try:
            data = _promotion_form_data()

            promo.product_id = data["product"].id
            promo.sale_price = data["sale_price"]
            promo.starts_at = data["starts_at"]
            promo.ends_at = data["ends_at"]
            promo.is_active = data["is_active"]

            db.session.commit()

            flash(
                f'Promoção de "{data["product"].name}" atualizada com sucesso!',
                "success"
            )

            return redirect(
                url_for(
                    "admin.admin_dashboard",
                    _anchor="promotions"
                )
            )

        except ValueError as error:
            db.session.rollback()
            flash(str(error), "danger")

        except IntegrityError:
            db.session.rollback()
            flash(
                "Não foi possível atualizar a promoção. "
                "Verifique os dados informados.",
                "danger"
            )

        except Exception as error:
            db.session.rollback()
            current_app.logger.error("Erro ao editar promoção: %s", error)
            flash(
                "Ocorreu um erro ao atualizar a promoção.",
                "danger"
            )

    return render_template(
        "admin/promotions/edit_promotion.html",
        promotion=promo,
        products=products,
    )


# -------------------------------------------------------------------
# Excluir promoção
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/delete_promotion/<uuid:id>", methods=["POST"])
@admin_required
def delete_promotion(id):

    promo = Promotion.query.get_or_404(id)

    try:
        product_name = (
            promo.product.name
            if promo.product
            else "Produto"
        )

        db.session.delete(promo)
        db.session.commit()

        flash(
            f'Promoção de "{product_name}" excluída com sucesso!',
            "success"
        )

    except Exception as error:
        db.session.rollback()
        current_app.logger.error("Erro ao excluir promoção: %s", error)
        flash(
            "Não foi possível excluir a promoção.",
            "danger"
        )

    return redirect(
        url_for(
            "admin.admin_dashboard",
            _anchor="promotions"
        )
    )


# -------------------------------------------------------------------
# Adicionar cupom
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/coupons/add", methods=["GET", "POST"])
@admin_required
def add_coupon():
    if request.method == "POST":
        try:
            data = _coupon_form_data()

            coupon = Coupon(
                code=data["code"],
                description=data["description"],
                discount_type=data["discount_type"],
                discount_value=data["discount_value"],
                minimum_order_amount=data["minimum_order_amount"],
                maximum_discount_amount=data["maximum_discount_amount"],
                starts_at=data["starts_at"],
                ends_at=data["ends_at"],
                usage_limit=data["usage_limit"],
                times_used=0,
                first_purchase_only=data["first_purchase_only"],
                is_active=data["is_active"],
            )

            db.session.add(coupon)
            db.session.commit()

            flash("Cupom criado com sucesso!", "success")
            return redirect(
                url_for("admin.admin_dashboard", _anchor="coupons")
            )

        except ValueError as error:
            db.session.rollback()
            flash(str(error), "danger")

        except IntegrityError:
            db.session.rollback()
            flash(
                "Não foi possível criar o cupom. Verifique se o código já existe.",
                "danger",
            )

        except Exception as error:
            db.session.rollback()
            current_app.logger.error("Erro ao criar cupom: %s", error)
            flash("Ocorreu um erro ao criar o cupom.", "danger")

    return render_template("admin/cupons/add_coupon.html")


# -------------------------------------------------------------------
# Editar cupom
# -------------------------------------------------------------------

@admin_blueprint.route(
    "/admin/coupons/edit/<uuid:id>",
    methods=["GET", "POST"],
)
@admin_required
def edit_coupon(id):
    coupon = Coupon.query.get_or_404(id)

    if request.method == "POST":
        try:
            data = _coupon_form_data(current_coupon=coupon)

            coupon.code = data["code"]
            coupon.description = data["description"]
            coupon.discount_type = data["discount_type"]
            coupon.discount_value = data["discount_value"]
            coupon.minimum_order_amount = data["minimum_order_amount"]
            coupon.maximum_discount_amount = data["maximum_discount_amount"]
            coupon.starts_at = data["starts_at"]
            coupon.ends_at = data["ends_at"]
            coupon.usage_limit = data["usage_limit"]
            coupon.first_purchase_only = data["first_purchase_only"]
            coupon.is_active = data["is_active"]

            db.session.commit()

            flash("Cupom atualizado com sucesso!", "success")
            return redirect(
                url_for("admin.admin_dashboard", _anchor="coupons")
            )

        except ValueError as error:
            db.session.rollback()
            flash(str(error), "danger")

        except IntegrityError:
            db.session.rollback()
            flash(
                "Não foi possível atualizar o cupom. "
                "Verifique o código e os limites informados.",
                "danger",
            )

        except Exception as error:
            db.session.rollback()
            current_app.logger.error("Erro ao editar cupom: %s", error)
            flash("Ocorreu um erro ao atualizar o cupom.", "danger")

    return render_template(
        "admin/cupons/edit_coupon.html",
        coupon=coupon,
    )


# -------------------------------------------------------------------
# Excluir cupom
# -------------------------------------------------------------------

@admin_blueprint.route(
    "/admin/coupons/delete/<uuid:id>",
    methods=["POST"],
)
@admin_required
def delete_coupon(id):
    coupon = Coupon.query.get_or_404(id)

    try:
        # Preserva cupons já utilizados para não perder o histórico.
        if coupon.times_used > 0:
            coupon.is_active = False
            db.session.commit()

            flash(
                "O cupom já possui utilizações e foi desativado "
                "em vez de ser excluído.",
                "warning",
            )
        else:
            code = coupon.code
            db.session.delete(coupon)
            db.session.commit()

            flash(f"Cupom {code} excluído com sucesso!", "success")

    except Exception as error:
        db.session.rollback()
        current_app.logger.error("Erro ao excluir cupom: %s", error)
        flash("Não foi possível excluir o cupom.", "danger")

    return redirect(
        url_for("admin.admin_dashboard", _anchor="coupons")
    )


# -------------------------------------------------------------------
# Adicionar fornecedor
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_supplier", methods=["GET", "POST"])
@admin_required
def add_supplier():

    if request.method == "POST":

        name         = request.form.get("name", "").strip()
        company_name = request.form.get("company_name", "").strip()
        phone        = request.form.get("phone", "").strip()
        email        = request.form.get("email", "").strip()
        website      = request.form.get("website", "").strip()
        address      = request.form.get("address", "").strip()

        try:
            supplier = Supplier(
                name         = name,
                company_name = company_name or None,
                phone        = phone        or None,
                email        = email        or None,
                website      = website      or None,
                address      = address      or None,
            )
            db.session.add(supplier)
            db.session.commit()
            flash("Fornecedor adicionado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="suppliers"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/suppliers/add_supplier.html")


# -------------------------------------------------------------------
# Editar fornecedor
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/edit_supplier/<uuid:id>", methods=["GET", "POST"])
@admin_required
def edit_supplier(id):

    supplier = Supplier.query.get_or_404(id)

    if request.method == "POST":

        supplier.name         = request.form.get("name", "").strip()
        supplier.company_name = request.form.get("company_name") or None
        supplier.phone        = request.form.get("phone")        or None
        supplier.email        = request.form.get("email")        or None
        supplier.website      = request.form.get("website")      or None
        supplier.address      = request.form.get("address")      or None

        try:
            db.session.commit()
            flash("Fornecedor atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="suppliers"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/suppliers/edit_supplier.html", supplier=supplier)


# -------------------------------------------------------------------
# Excluir fornecedor
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/delete_supplier/<uuid:id>", methods=["POST"])
@admin_required
def delete_supplier(id):

    supplier = Supplier.query.get_or_404(id)
    db.session.delete(supplier)
    db.session.commit()
    flash("Fornecedor excluído com sucesso!", "success")
    return redirect(url_for("admin.admin_dashboard", _anchor="suppliers"))


# -------------------------------------------------------------------
# Adicionar testemunho
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_testimonial", methods=["GET", "POST"])
@admin_required
def add_testimonial():

    if request.method == "POST":

        author = request.form.get("author", "").strip()
        text   = request.form.get("text", "").strip()
        rating = request.form.get("rating", "5").strip()

        try:
            testimonial = Testimonial(
                author     = author or None,
                text       = text   or None,
                rating     = int(rating),
                is_visible = True,
            )
            db.session.add(testimonial)
            db.session.commit()
            flash("Depoimento adicionado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="testimonials"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/testimonials/add_testimonial.html")


# -------------------------------------------------------------------
# Editar testemunho
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/edit_testimonial/<uuid:id>", methods=["GET", "POST"])
@admin_required
def edit_testimonial(id):

    testimonial = Testimonial.query.get_or_404(id)

    if request.method == "POST":

        testimonial.author     = request.form.get("author") or None
        testimonial.text       = request.form.get("text")   or None
        testimonial.rating     = int(request.form.get("rating", 5))
        testimonial.is_visible = request.form.get("is_visible") == "on"

        try:
            db.session.commit()
            flash("Depoimento atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="testimonials"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template(
        "admin/testimonials/edit_testimonial.html",
        testimonial=testimonial
    )


# -------------------------------------------------------------------
# Excluir testemunho
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/delete_testimonial/<uuid:id>", methods=["POST"])
@admin_required
def delete_testimonial(id):

    testimonial = Testimonial.query.get_or_404(id)
    db.session.delete(testimonial)
    db.session.commit()
    flash("Depoimento excluído com sucesso!", "success")
    return redirect(url_for("admin.admin_dashboard", _anchor="testimonials"))


# -------------------------------------------------------------------
# Adicionar hero slide
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_hero_slide", methods=["GET", "POST"])
@admin_required
def add_hero_slide():

    if request.method == "POST":

        image   = request.form.get("image", "").strip()
        alt     = request.form.get("alt", "").strip()
        eyebrow = request.form.get("eyebrow", "").strip()
        title   = request.form.get("title", "").strip()

        try:
            slide = HeroSlide(
                image   = image,
                alt     = alt     or None,
                eyebrow = eyebrow or None,
                title   = title   or None,
            )
            db.session.add(slide)
            db.session.commit()
            flash("Slide adicionado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="hero"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/hero/add_hero_slide.html")


# -------------------------------------------------------------------
# Editar hero slide
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/edit_hero_slide/<uuid:id>", methods=["GET", "POST"])
@admin_required
def edit_hero_slide(id):

    slide = HeroSlide.query.get_or_404(id)

    if request.method == "POST":

        slide.image   = request.form.get("image", "").strip()
        slide.alt     = request.form.get("alt")     or None
        slide.eyebrow = request.form.get("eyebrow") or None
        slide.title   = request.form.get("title")   or None

        try:
            db.session.commit()
            flash("Slide atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="hero"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/hero/edit_hero_slide.html", slide=slide)


# -------------------------------------------------------------------
# Excluir hero slide
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/delete_hero_slide/<uuid:id>", methods=["POST"])
@admin_required
def delete_hero_slide(id):

    slide = HeroSlide.query.get_or_404(id)
    db.session.delete(slide)
    db.session.commit()
    flash("Slide excluído com sucesso!", "success")
    return redirect(url_for("admin.admin_dashboard", _anchor="hero"))


# -------------------------------------------------------------------
# Adicionar / editar configurações do site  (singleton)
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_site_config", methods=["GET", "POST"])
@admin_required
def add_site_config():

    # se já existe um registro, redireciona para edição
    existing = SiteConfig.query.first()
    if existing:
        return redirect(url_for("admin.edit_site_config", _anchor="siteconfig"))

    if request.method == "POST":

        try:
            config = SiteConfig(
                name             = request.form.get("name", "").strip(),
                meta_description = request.form.get("meta_description") or None,
                banner_quote     = request.form.get("banner_quote")     or None,
                banner_cta_text  = request.form.get("banner_cta_text")  or None,
                promo_badge      = request.form.get("promo_badge")      or None,
                copyright_year   = int(request.form.get("copyright_year") or 0) or None,
                copyright_text   = request.form.get("copyright_text")   or None,
            )
            db.session.add(config)
            db.session.commit()
            flash("Configurações do site salvas com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="siteconfig"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/config/add_site_config.html")


@admin_blueprint.route("/admin/edit_site_config", methods=["GET", "POST"])
@admin_required
def edit_site_config():

    config = SiteConfig.query.first()

    if not config:
        return redirect(url_for("admin.add_site_config"))

    if request.method == "POST":

        config.name             = request.form.get("name", "").strip()
        config.meta_description = request.form.get("meta_description") or None
        config.banner_quote     = request.form.get("banner_quote")     or None
        config.banner_cta_text  = request.form.get("banner_cta_text")  or None
        config.promo_badge      = request.form.get("promo_badge")      or None
        config.copyright_text   = request.form.get("copyright_text")   or None

        year = request.form.get("copyright_year", "").strip()
        config.copyright_year = int(year) if year.isdigit() else None

        try:
            db.session.commit()
            flash("Configurações do site atualizadas com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="siteconfig"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/config/edit_site_config.html", config=config)


# -------------------------------------------------------------------
# Adicionar link de rede social
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_social_link", methods=["GET", "POST"])
@admin_required
def add_social_link():

    if request.method == "POST":

        label = request.form.get("label", "").strip()
        url   = request.form.get("url", "").strip()
        icon  = request.form.get("icon", "").strip()

        if not _footer_url_ok(url):
            flash(_FOOTER_URL_ERROR, "danger")
            return render_template("admin/config/add_social_link.html")

        try:
            social = SocialLink(
                label = label,
                url   = url  or None,
                icon  = icon or None,
            )
            db.session.add(social)
            db.session.commit()
            invalidate_footer_cache()
            flash("Rede social adicionada com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/config/add_social_link.html")


# -------------------------------------------------------------------
# Editar link de rede social
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/edit_social_link/<uuid:id>", methods=["GET", "POST"])
@admin_required
def edit_social_link(id):

    social = SocialLink.query.get_or_404(id)

    if request.method == "POST":

        new_url = (request.form.get("url") or "").strip()
        if not _footer_url_ok(new_url):
            flash(_FOOTER_URL_ERROR, "danger")
            return render_template("admin/config/edit_social_link.html", social=social)

        social.label = request.form.get("label", "").strip()
        social.url   = new_url
        social.icon  = request.form.get("icon") or None

        try:
            db.session.commit()
            invalidate_footer_cache()
            flash("Rede social atualizada com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/config/edit_social_link.html", social=social)


# -------------------------------------------------------------------
# Excluir link de rede social
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/delete_social_link/<uuid:id>", methods=["POST"])
@admin_required
def delete_social_link(id):

    social = SocialLink.query.get_or_404(id)
    db.session.delete(social)
    db.session.commit()
    invalidate_footer_cache()
    flash("Rede social excluída com sucesso!", "success")
    return redirect(url_for("admin.admin_dashboard", _anchor="footer"))


# -------------------------------------------------------------------
# Adicionar / editar configuração do rodapé  (singleton)
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_footer_config", methods=["GET", "POST"])
@admin_required
def add_footer_config():

    existing = FooterConfig.query.first()
    if existing:
        return redirect(url_for("admin.edit_footer_config"))

    if request.method == "POST":

        try:
            footer = FooterConfig(
                brand_name        = request.form.get("brand_name", "").strip(),
                brand_description = request.form.get("brand_description") or None,
            )
            db.session.add(footer)
            db.session.commit()
            invalidate_footer_cache()
            flash("Configuração do rodapé salva com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/footer/add_footer_config.html")


@admin_blueprint.route("/admin/edit_footer_config", methods=["GET", "POST"])
@admin_required
def edit_footer_config():

    footer = FooterConfig.query.first()

    if not footer:
        return redirect(url_for("admin.add_footer_config", _anchor="footer"))

    if request.method == "POST":

        footer.brand_name        = request.form.get("brand_name", "").strip()
        footer.brand_description = request.form.get("brand_description") or None

        try:
            db.session.commit()
            invalidate_footer_cache()
            flash("Rodapé atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/footer/edit_footer_config.html", footer=footer)


# -------------------------------------------------------------------
# Adicionar / editar informações de contato  (singleton)
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_contact_info", methods=["GET", "POST"])
@admin_required
def add_contact_info():

    existing = ContactInfo.query.first()
    if existing:
        return redirect(url_for("admin.edit_contact_info"))

    if request.method == "POST":

        try:
            contact = ContactInfo(
                address = request.form.get("address") or None,
                phone   = request.form.get("phone")   or None,
                email   = request.form.get("email")   or None,
                hours   = request.form.get("hours")   or None,
            )
            db.session.add(contact)
            db.session.commit()
            invalidate_footer_cache()
            flash("Informações de contato salvas com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/footer/add_contact_info.html")


@admin_blueprint.route("/admin/edit_contact_info", methods=["GET", "POST"])
@admin_required
def edit_contact_info():

    contact = ContactInfo.query.first()

    if not contact:
        return redirect(url_for("admin.add_contact_info", _anchor="footer"))

    if request.method == "POST":

        contact.address = request.form.get("address") or None
        contact.phone   = request.form.get("phone")   or None
        contact.email   = request.form.get("email")   or None
        contact.hours   = request.form.get("hours")   or None

        try:
            db.session.commit()
            invalidate_footer_cache()
            flash("Contato atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/footer/edit_contact_info.html", contact=contact)


# -------------------------------------------------------------------
# Adicionar link de navegação
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_nav_link", methods=["GET", "POST"])
@admin_required
def add_nav_link():

    if request.method == "POST":

        label = request.form.get("label", "").strip()
        url   = request.form.get("url", "").strip()

        if not _footer_url_ok(url):
            flash(_FOOTER_URL_ERROR, "danger")
            return render_template("admin/footer/add_nav_link.html")

        try:
            nav = NavLink(label=label, url=url)
            db.session.add(nav)
            db.session.commit()
            invalidate_footer_cache()
            flash("Link de navegação adicionado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/footer/add_nav_link.html")


# -------------------------------------------------------------------
# Editar link de navegação
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/edit_nav_link/<uuid:id>", methods=["GET", "POST"])
@admin_required
def edit_nav_link(id):

    nav = NavLink.query.get_or_404(id)

    if request.method == "POST":

        new_url = request.form.get("url", "").strip()
        if not _footer_url_ok(new_url):
            flash(_FOOTER_URL_ERROR, "danger")
            return render_template("admin/footer/edit_nav_link.html", nav=nav)

        nav.label = request.form.get("label", "").strip()
        nav.url   = new_url

        try:
            db.session.commit()
            invalidate_footer_cache()
            flash("Link atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/footer/edit_nav_link.html", nav=nav)


# -------------------------------------------------------------------
# Excluir link de navegação
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/delete_nav_link/<uuid:id>", methods=["POST"])
@admin_required
def delete_nav_link(id):

    nav = NavLink.query.get_or_404(id)
    db.session.delete(nav)
    db.session.commit()
    invalidate_footer_cache()
    flash("Link excluído com sucesso!", "success")
    return redirect(url_for("admin.admin_dashboard", _anchor="footer"))


# -------------------------------------------------------------------
# Adicionar link de categoria
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_category_link", methods=["GET", "POST"])
@admin_required
def add_category_link():

    if request.method == "POST":

        label = request.form.get("label", "").strip()
        url   = request.form.get("url", "").strip()
        slug = request.form.get("slug", "").strip()
        image = request.form.get("image", "").strip()

        if not _footer_url_ok(url):
            flash(_FOOTER_URL_ERROR, "danger")
            return render_template("admin/footer/add_category_link.html")

        try:
            cat = CategoryLink(label=label, url=url, slug=slug, image=image)
            db.session.add(cat)
            db.session.commit()
            invalidate_footer_cache()
            flash("Link de categoria adicionado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            current_app.logger.error("Erro: %s", e)
            flash(str(e), "danger")

    return render_template("admin/footer/add_category_link.html")


# -------------------------------------------------------------------
# Editar link de categoria
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/edit_category_link/<uuid:id>", methods=["GET", "POST"])
@admin_required
def edit_category_link(id):

    cat = CategoryLink.query.get_or_404(id)

    if request.method == "POST":

        new_url = request.form.get("url", "").strip()
        if not _footer_url_ok(new_url):
            flash(_FOOTER_URL_ERROR, "danger")
            return render_template("admin/footer/edit_category_link.html", cat=cat)

        cat.label = request.form.get("label", "").strip()
        cat.url   = new_url
        cat.slug = request.form.get("slug", "").strip()
        cat.image = request.form.get("image", "").strip()

        try:
            db.session.commit()
            invalidate_footer_cache()
            flash("Link de categoria atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/footer/edit_category_link.html", cat=cat)


# -------------------------------------------------------------------
# Excluir link de categoria
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/delete_category_link/<uuid:id>", methods=["POST"])
@admin_required
def delete_category_link(id):

    cat = CategoryLink.query.get_or_404(id)
    db.session.delete(cat)
    db.session.commit()
    invalidate_footer_cache()
    flash("Link de categoria excluído com sucesso!", "success")
    return redirect(url_for("admin.admin_dashboard", _anchor="footer"))


# -------------------------------------------------------------------
# Adicionar link legal
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/add_legal_link", methods=["GET", "POST"])
@admin_required
def add_legal_link():

    if request.method == "POST":

        label = request.form.get("label", "").strip()
        url   = request.form.get("url", "").strip()

        if not _footer_url_ok(url):
            flash(_FOOTER_URL_ERROR, "danger")
            return render_template("admin/footer/add_legal_link.html")

        try:
            legal = LegalLink(label=label, url=url)
            db.session.add(legal)
            db.session.commit()
            invalidate_footer_cache()
            flash("Link legal adicionado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/footer/add_legal_link.html")


# -------------------------------------------------------------------
# Editar link legal
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/edit_legal_link/<uuid:id>", methods=["GET", "POST"])
@admin_required
def edit_legal_link(id):

    legal = LegalLink.query.get_or_404(id)

    if request.method == "POST":

        new_url = request.form.get("url", "").strip()
        if not _footer_url_ok(new_url):
            flash(_FOOTER_URL_ERROR, "danger")
            return render_template("admin/footer/edit_legal_link.html", legal=legal)

        legal.label = request.form.get("label", "").strip()
        legal.url   = new_url

        try:
            db.session.commit()
            invalidate_footer_cache()
            flash("Link legal atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="footer"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/footer/edit_legal_link.html", legal=legal)


# -------------------------------------------------------------------
# Excluir link legal
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/delete_legal_link/<uuid:id>", methods=["POST"])
@admin_required
def delete_legal_link(id):

    legal = LegalLink.query.get_or_404(id)
    db.session.delete(legal)
    db.session.commit()
    invalidate_footer_cache()
    flash("Link legal excluído com sucesso!", "success")
    return redirect(url_for("admin.admin_dashboard", _anchor="footer"))


# -------------------------------------------------------------------
# Adicionar usuário
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/users/add", methods=["GET", "POST"])
@admin_required
def add_user():

    if request.method == "POST":

        user = User(
            name     = request.form.get("name"),
            email    = request.form.get("email"),
            password = generate_password_hash(request.form.get("password")),
            phone    = request.form.get("phone") or None,
            is_admin = request.form.get("is_admin") == "on",
        )

        try:
            db.session.add(user)
            db.session.commit()
            flash("Usuário criado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard"))

        except Exception as e:
            db.session.rollback()
            flash(str(e), "danger")

    return render_template("admin/users/add_user.html", _anchor="users")


# -------------------------------------------------------------------
# Editar usuário
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/users/edit/<uuid:id>", methods=["GET", "POST"])
@login_required
@admin_required
def edit_user(id):

    user = User.query.get_or_404(id)

    if user.id == current_user.id:
        flash("Edite sua própria conta pela página de perfil.", "warning")
        return redirect(url_for("admin.admin_dashboard", _anchor="users"))

    if request.method == "POST":

        name     = request.form.get("name", "").strip()
        email    = request.form.get("email", "").strip()
        phone    = request.form.get("phone", "").strip()
        password = request.form.get("password", "").strip()
        is_admin = request.form.get("is_admin") == "on"

        if not name or not email:
            flash("Nome e e-mail são obrigatórios.", "danger")
            return redirect(url_for("admin.edit_user", id=user.id))

        email_exists = User.query.filter(
            User.email == email,
            User.id    != user.id
        ).first()

        if email_exists:
            flash("Já existe um usuário com este e-mail.", "danger")
            return redirect(url_for("admin.edit_user", id=user.id))

        try:
            user.name     = name
            user.email    = email
            user.phone    = phone or None
            user.is_admin = is_admin

            if password:
                user.password = generate_password_hash(password)

            db.session.commit()
            flash("Usuário atualizado com sucesso!", "success")
            return redirect(url_for("admin.admin_dashboard", _anchor="users"))

        except Exception as e:
            db.session.rollback()
            flash(f"Erro ao atualizar usuário: {e}", "danger")

    return render_template("admin/users/edit_user.html", user=user)


# -------------------------------------------------------------------
# Excluir usuário
# -------------------------------------------------------------------

@admin_blueprint.route("/admin/users/delete/<uuid:id>", methods=["POST"])
@admin_required
def delete_user(id):

    user = User.query.get_or_404(id)
    db.session.delete(user)
    db.session.commit()
    flash("Usuário excluído com sucesso!", "success")
    return redirect(url_for("admin.admin_dashboard", _anchor="users"))
