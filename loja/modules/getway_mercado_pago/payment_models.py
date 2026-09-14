import uuid

from flask import has_request_context, request
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Session

from loja import db
from loja.utils import utcnow


def generate_order_number():
    """
    Gera um identificador público, curto e independente do UUID interno.

    Exemplo:
        PM-260818-A1B2C3D4

    Estrutura:
        PM        -> prefixo da Pollo Modas
        260818    -> data UTC no formato AAMMDD
        A1B2C3D4  -> sufixo aleatório derivado de UUID

    A coluna possui UNIQUE no banco, portanto uma eventual colisão também
    será bloqueada pela constraint de unicidade.
    """
    date_part = utcnow().strftime("%y%m%d")
    random_part = uuid.uuid4().hex[:8].upper()
    return f"PM-{date_part}-{random_part}"


class Order(db.Model):
    """
    Pedido interno.

    `id` continua sendo a chave primária técnica (UUID).
    `order_number` é o identificador público exibido ao cliente.

    O Mercado Pago pode continuar referenciando `id` por
    external_reference, sem expor a chave interna na interface.
    """

    __tablename__ = "orders"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    order_number = db.Column(
        db.String(32),
        nullable=False,
        unique=True,
        index=True,
        default=generate_order_number,
    )

    # A FK é declarada como "users.id" (sem schema) e resolve para o schema
    # dedicado da PolloModas via search_path (pollomodas). O schema `public`
    # pertence ao sistema legado e é intocável; a constraint orders_user_id_fkey
    # deve apontar para pollomodas.users(id), nunca para public.users(id).
    user_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey(
            "users.id",
            name="orders_user_id_fkey",
            ondelete="RESTRICT",
        ),
        nullable=False,
        index=True,
    )

    coupon_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey(
            "coupons.id",
            name="orders_coupon_id_fkey",
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
    )

    checkout_type = db.Column(
        db.String(20),
        nullable=False,
    )

    status = db.Column(
        db.String(30),
        nullable=False,
        default="pending_payment",
        server_default="pending_payment",
        index=True,
    )

    subtotal = db.Column(
        db.Numeric(10, 2),
        nullable=False,
    )

    shipping = db.Column(
        db.Numeric(10, 2),
        nullable=False,
        default=0,
        server_default="0",
    )

    discount = db.Column(
        db.Numeric(10, 2),
        nullable=False,
        default=0,
        server_default="0",
    )

    total = db.Column(
        db.Numeric(10, 2),
        nullable=False,
    )

    coupon_code = db.Column(db.String(50))
    coupon_counted_at = db.Column(db.DateTime)

    customer_name = db.Column(db.String(160), nullable=False)
    customer_email = db.Column(db.String(160), nullable=False)
    customer_phone = db.Column(db.String(30))
    # FASE 8.0.12C: nullable para preservar pedidos legados; novos checkouts
    # AliExpress normalizam/validam antes de qualquer chamada ao Mercado Pago.
    customer_cpf = db.Column(db.String(11), nullable=True)

    zip_code = db.Column(db.String(8), nullable=False)
    street = db.Column(db.String(255), nullable=False)
    number = db.Column(db.String(30), nullable=False)
    complement = db.Column(db.String(120))
    neighborhood = db.Column(db.String(120), nullable=False)
    city = db.Column(db.String(120), nullable=False)
    state = db.Column(db.String(2), nullable=False)

    mp_preference_id = db.Column(
        db.String(100),
        unique=True,
        index=True,
    )
    mp_checkout_url = db.Column(db.Text)
    mp_payment_id = db.Column(db.String(100), index=True)
    mp_status = db.Column(db.String(50))
    mp_status_detail = db.Column(db.String(120))

    manual_review_reason = db.Column(db.Text)
    paid_at = db.Column(db.DateTime)

    # FASE 9.0.0: origem de tráfego sanitizada e opcional; nunca expõe
    # payload bruto da URL. Pedidos legados permanecem NULL (fail-closed).
    acquisition_source = db.Column(db.String(64), nullable=True)
    acquisition_medium = db.Column(db.String(64), nullable=True)
    acquisition_campaign = db.Column(db.String(120), nullable=True)
    acquisition_referrer = db.Column(db.String(120), nullable=True)

    # FASE 8.3.0-H: ledger financeiro local; não substitui a autoridade
    # do webhook Mercado Pago sobre Order.status == "refunded".
    supplier_refund_state = db.Column(db.String(30), nullable=True)
    supplier_refund_requested_at = db.Column(db.DateTime, nullable=True)
    supplier_refund_confirmed_at = db.Column(db.DateTime, nullable=True)
    supplier_refund_last_error = db.Column(db.String(64), nullable=True)

    # Estoque já reservado (abatido) na criação do pedido para evitar
    # overselling enquanto o pagamento está pendente.
    stock_reserved = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utcnow,
        server_default=db.func.now(),
    )

    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utcnow,
        server_default=db.func.now(),
        onupdate=utcnow,
    )

    user = db.relationship("User", lazy="joined")
    coupon = db.relationship("Coupon", lazy="joined")

    items = db.relationship(
        "OrderItem",
        back_populates="order",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="OrderItem.created_at",
    )

    payments = db.relationship(
        "Payment",
        back_populates="order",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Payment.created_at",
    )

    shipments = db.relationship(
        "Shipment",
        back_populates="order",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Shipment.created_at",
    )

    __table_args__ = (
        db.CheckConstraint(
            "checkout_type IN ('cart', 'product')",
            name="ck_orders_checkout_type",
        ),
        db.CheckConstraint(
            "subtotal >= 0",
            name="ck_orders_subtotal_nonnegative",
        ),
        db.CheckConstraint(
            "shipping >= 0",
            name="ck_orders_shipping_nonnegative",
        ),
        db.CheckConstraint(
            "discount >= 0",
            name="ck_orders_discount_nonnegative",
        ),
        db.CheckConstraint(
            "total >= 0",
            name="ck_orders_total_nonnegative",
        ),
        db.CheckConstraint(
            "customer_cpf IS NULL OR length(customer_cpf) = 11",
            name="ck_orders_customer_cpf_length",
        ),
        db.CheckConstraint(
            "supplier_refund_state IS NULL OR supplier_refund_state IN "
            "('requested','pending_webhook','confirmed','failed','manual_review')",
            name="ck_orders_supplier_refund_state",
        ),
        db.Index(
            "ix_orders_user_created",
            "user_id",
            "created_at",
        ),
    )

    def __repr__(self):
        return (
            f"<Order {self.order_number} id={self.id} "
            f"status={self.status} total={self.total}>"
        )


class OrderItem(db.Model):
    """
    Snapshot do produto comprado.

    Alterações futuras no catálogo não modificam o pedido já criado.
    """

    __tablename__ = "order_items"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    order_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey(
            "orders.id",
            name="order_items_order_id_fkey",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    product_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey(
            "products.id",
            name="order_items_product_id_fkey",
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
    )

    product_name = db.Column(db.String(255), nullable=False)
    selected_size = db.Column(db.String(20))
    variant_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("product_variants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    line_total = db.Column(db.Numeric(10, 2), nullable=False)

    # FASE 8.2.0-D: snapshot financeiro unitário no checkout. Campos
    # nullable preservam pedidos legados e mantêm custo desconhecido como NULL.
    supplier_unit_cost_snapshot = db.Column(db.Numeric(10, 2), nullable=True)
    supplier_shipping_cost_snapshot = db.Column(db.Numeric(10, 2), nullable=True)
    supplier_landed_cost_snapshot = db.Column(db.Numeric(10, 2), nullable=True)
    supplier_cost_currency_snapshot = db.Column(db.String(3), nullable=True)

    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utcnow,
        server_default=db.func.now(),
    )

    order = db.relationship("Order", back_populates="items")
    product = db.relationship("Product", lazy="joined")
    variant = db.relationship("ProductVariant", back_populates="order_items")

    __table_args__ = (
        db.CheckConstraint(
            "quantity >= 1",
            name="ck_order_items_quantity_positive",
        ),
        db.CheckConstraint(
            "unit_price >= 0",
            name="ck_order_items_unit_price_nonnegative",
        ),
        db.CheckConstraint(
            "line_total >= 0",
            name="ck_order_items_line_total_nonnegative",
        ),
        db.CheckConstraint(
            "supplier_unit_cost_snapshot IS NULL OR "
            "supplier_unit_cost_snapshot > 0",
            name="ck_order_items_supplier_unit_cost_positive",
        ),
        db.CheckConstraint(
            "supplier_shipping_cost_snapshot IS NULL OR "
            "supplier_shipping_cost_snapshot >= 0",
            name="ck_order_items_supplier_shipping_nonnegative",
        ),
        db.CheckConstraint(
            "supplier_landed_cost_snapshot IS NULL OR "
            "supplier_landed_cost_snapshot > 0",
            name="ck_order_items_supplier_landed_cost_positive",
        ),
    )

    def __repr__(self):
        return (
            f"<OrderItem {self.id} product={self.product_id} "
            f"quantity={self.quantity}>"
        )


class Payment(db.Model):
    """
    Pagamento retornado pelo Mercado Pago.

    provider_payment_id é único para impedir processamento duplicado.
    """

    __tablename__ = "payments"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    order_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey(
            "orders.id",
            name="payments_order_id_fkey",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    provider = db.Column(
        db.String(30),
        nullable=False,
        default="mercado_pago",
        server_default="mercado_pago",
    )

    provider_payment_id = db.Column(
        db.String(100),
        nullable=False,
        unique=True,
        index=True,
    )

    status = db.Column(
        db.String(50),
        nullable=False,
        index=True,
    )

    status_detail = db.Column(db.String(120))
    amount = db.Column(db.Numeric(10, 2), nullable=False)

    currency = db.Column(
        db.String(3),
        nullable=False,
        default="BRL",
        server_default="BRL",
    )

    payment_method_id = db.Column(db.String(50))
    payment_type_id = db.Column(db.String(50))

    raw_payload = db.Column(
        db.JSON,
        nullable=False,
        default=dict,
    )

    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utcnow,
        server_default=db.func.now(),
    )

    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utcnow,
        server_default=db.func.now(),
        onupdate=utcnow,
    )

    order = db.relationship(
        "Order",
        back_populates="payments",
    )

    __table_args__ = (
        db.CheckConstraint(
            "amount >= 0",
            name="ck_payments_amount_nonnegative",
        ),
    )

    def __repr__(self):
        return (
            f"<Payment {self.provider_payment_id} "
            f"status={self.status} amount={self.amount}>"
        )


def _order_requires_aliexpress_phone(session, order):
    """Retorna True quando o pedido novo contém ao menos um produto AliExpress."""
    unresolved_product_ids = set()

    for item in list(order.items):
        product = getattr(item, "product", None)
        if product is not None:
            if getattr(product, "source", None) == "aliexpress":
                return True
            continue
        if item.product_id is not None:
            unresolved_product_ids.add(item.product_id)

    if not unresolved_product_ids:
        return False

    # Import tardio evita ciclo entre models.py e payment_models.py.
    from loja.modules.models import Product

    return (
        session.query(Product.id)
        .filter(
            Product.id.in_(unresolved_product_ids),
            Product.source == "aliexpress",
        )
        .first()
        is not None
    )


def _normalize_order_phone_for_checkout(order, *, requires_aliexpress):
    """Normaliza celular BR somente quando o fulfillment AliExpress exige."""
    if not requires_aliexpress:
        return order.customer_phone

    from loja.integrations.aliexpress.submission_snapshot import (
        SubmissionSnapshotError,
        _normalize_br_mobile,
    )

    try:
        normalized = _normalize_br_mobile(order.customer_phone)
    except SubmissionSnapshotError as error:
        raise ValueError(
            "Informe um celular brasileiro válido com DDD."
        ) from error

    order.customer_phone = normalized
    return normalized


def _normalize_order_cpf_for_checkout(order, *, requires_aliexpress, raw_cpf=None):
    """Normaliza CPF de checkout AliExpress sem ecoar PII em erros.

    Pedidos não AliExpress permanecem retrocompatíveis. Para AliExpress, o CPF
    precisa existir e ter checksum válido antes do flush que antecede a criação
    da preferência no Mercado Pago.
    """
    if not requires_aliexpress:
        return order.customer_cpf

    from loja.utils.br_tax_id import BrazilianTaxIdError, normalize_br_cpf

    candidate = raw_cpf if raw_cpf is not None else order.customer_cpf
    try:
        normalized = normalize_br_cpf(candidate)
    except BrazilianTaxIdError as error:
        raise ValueError("Informe um CPF válido.") from error

    order.customer_cpf = normalized
    return normalized


@event.listens_for(Session, "before_flush")
def _guard_aliexpress_checkout_phone(session, flush_context, instances):
    """Falha fechado antes do pagamento quando dados AliExpress são inválidos.

    O endpoint de pagamento já envolve reserva de estoque, criação do Order e
    ``flush`` na mesma transação. Se este guard falhar, o handler existente faz
    ``rollback()``, desfazendo a reserva integralmente antes de qualquer chamada
    a ``create_preference``/Mercado Pago.
    """
    if not has_request_context():
        return
    if request.endpoint != "payments.create_mercado_pago_payment":
        return

    for obj in tuple(session.new):
        if not isinstance(obj, Order):
            continue
        requires_aliexpress = _order_requires_aliexpress_phone(session, obj)
        _normalize_order_phone_for_checkout(
            obj,
            requires_aliexpress=requires_aliexpress,
        )
        _normalize_order_cpf_for_checkout(
            obj,
            requires_aliexpress=requires_aliexpress,
            raw_cpf=request.form.get("cpf"),
        )
