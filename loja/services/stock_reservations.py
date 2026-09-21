"""Liberação idempotente de reservas com expiração comprovada.

A rotina nunca consulta nem cancela recursos no Mercado Pago. Somente pedidos
com prazo persistido, já vencido, ainda pending_payment e sem pagamento
canônico aprovado podem devolver estoque. Pedidos legados sem prazo falham
fechados.
"""

from dataclasses import dataclass

from flask import current_app

from loja import db
from loja.modules.getway_mercado_pago.payment_models import Order, Payment
from loja.modules.models import Product, ProductVariant
from loja.services.reservation_policy import (
    DEFAULT_RESERVATION_HOURS,
    load_reservation_hours,
)
from loja.utils import utcnow

DEFAULT_RELEASE_LIMIT = 100
MAX_RELEASE_LIMIT = 500


@dataclass(frozen=True)
class StockReservationReleaseResult:
    candidates: int
    released: int
    skipped: int
    hours: int
    source: str


def _bounded_int(value, *, default, minimum, maximum, code):
    if value is None or str(value).strip() == "":
        return default
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(code) from None
    if parsed < minimum or parsed > maximum:
        raise ValueError(code)
    return parsed


def load_release_limit(value=None):
    if value is None:
        value = current_app.config.get(
            "STOCK_RESERVATION_RELEASE_LIMIT",
            DEFAULT_RELEASE_LIMIT,
        )
    return _bounded_int(
        value,
        default=DEFAULT_RELEASE_LIMIT,
        minimum=1,
        maximum=MAX_RELEASE_LIMIT,
        code="stock_reservation_release_limit_invalid",
    )


def is_scheduled_release_enabled():
    return bool(
        current_app.config.get("STOCK_RESERVATION_RELEASE_ENABLED", False)
    )


def _latest_payment(order_id):
    return (
        Payment.query
        .filter_by(order_id=order_id)
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .first()
    )


def _release_eligible(order, *, now):
    if (
        order is None
        or not order.stock_reserved
        or order.status != "pending_payment"
        or order.mp_preference_expires_at is None
        or order.mp_preference_expires_at >= now
    ):
        return False
    payment = _latest_payment(order.id)
    return payment is None or payment.status != "approved"


def find_expired_stock_reservations(*, hours=None, limit=None, now=None):
    """Lista candidatos comprovadamente vencidos, sem mutação."""
    load_reservation_hours(hours)  # valida configuração; prazo persistido manda.
    limit = load_release_limit(limit)
    effective_now = now or utcnow()
    candidates = (
        Order.query
        .filter(
            Order.stock_reserved.is_(True),
            Order.status == "pending_payment",
            Order.mp_preference_expires_at.isnot(None),
            Order.mp_preference_expires_at < effective_now,
        )
        .order_by(Order.mp_preference_expires_at.asc(), Order.id.asc())
        .limit(limit)
        .all()
    )
    eligible = []
    for order in candidates:
        payment = _latest_payment(order.id)
        if payment is None or payment.status != "approved":
            eligible.append(order)
    return eligible


def _release_stock_locked(order):
    """Restitui integralmente uma reserva; requer o Order já travado."""
    if not order.stock_reserved or order.status != "pending_payment":
        return False

    for item in order.items:
        if item.variant_id is not None:
            if item.product_id is None:
                raise ValueError("stock_release_item_integrity_invalid")

            product = (
                Product.query
                .filter_by(id=item.product_id)
                .with_for_update()
                .first()
            )
            if product is None:
                raise ValueError("stock_release_product_missing")

            variant = (
                ProductVariant.query
                .filter_by(id=item.variant_id, product_id=item.product_id)
                .with_for_update()
                .first()
            )
            if variant is None:
                raise ValueError("stock_release_variant_invalid")

            if variant.stock is not None:
                variant.stock += item.quantity
            if product.stock is not None:
                product.stock += item.quantity
        else:
            if item.product_id is None:
                continue
            product = (
                Product.query
                .filter_by(id=item.product_id)
                .with_for_update()
                .first()
            )
            if product is not None and product.stock is not None:
                product.stock += item.quantity

    order.stock_reserved = False
    return True


def release_single_expired_stock_reservation(
    order_id, *, source="admin", now=None
):
    """Libera um pedido sob lock, aplicando todas as proteções canônicas."""
    effective_now = now or utcnow()
    try:
        order = (
            Order.query
            .filter_by(id=order_id)
            .with_for_update(of=Order)
            .first()
        )
        if not _release_eligible(order, now=effective_now):
            db.session.rollback()
            return False
        released = _release_stock_locked(order)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    current_app.logger.info(
        "stock_reservation_single_release source=%s released=%s",
        str(source or "admin")[:32],
        int(bool(released)),
    )
    return released


def release_expired_stock_reservations(
    *,
    hours=None,
    limit=None,
    source="service",
    now=None,
):
    """Libera reservas vencidas com lock e revalidação fail-closed."""
    hours = load_reservation_hours(hours)
    limit = load_release_limit(limit)
    effective_now = now or utcnow()
    source = str(source or "service")[:32]

    candidate_ids = [
        row[0]
        for row in (
            db.session.query(Order.id)
            .filter(
                Order.stock_reserved.is_(True),
                Order.status == "pending_payment",
                Order.mp_preference_expires_at.isnot(None),
                Order.mp_preference_expires_at < effective_now,
            )
            .order_by(Order.mp_preference_expires_at.asc(), Order.id.asc())
            .limit(limit)
            .all()
        )
    ]

    released = 0
    skipped = 0
    try:
        for order_id in candidate_ids:
            order = (
                Order.query
                .filter_by(id=order_id)
                .with_for_update(of=Order, skip_locked=True)
                .first()
            )
            if not _release_eligible(order, now=effective_now):
                skipped += 1
                continue
            if _release_stock_locked(order):
                released += 1
            else:
                skipped += 1

        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    current_app.logger.info(
        "stock_reservation_cleanup source=%s candidates=%s released=%s skipped=%s",
        source,
        len(candidate_ids),
        released,
        skipped,
    )
    return StockReservationReleaseResult(
        candidates=len(candidate_ids),
        released=released,
        skipped=skipped,
        hours=hours,
        source=source,
    )
