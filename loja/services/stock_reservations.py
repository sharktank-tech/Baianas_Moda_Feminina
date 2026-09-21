"""Liberação idempotente de reservas de estoque de checkouts abandonados.

A rotina nunca cancela pedido ou preferência no Mercado Pago. Ela apenas
restitui estoque quando o pedido continua pending_payment depois da janela
operacional. O pedido é travado antes de Produto/Variante, na mesma ordem usada
pelo processamento de pagamento, para serializar a corrida pagamento x limpeza.
"""

from dataclasses import dataclass
from datetime import timedelta

from flask import current_app

from loja import db
from loja.modules.getway_mercado_pago.payment_models import Order
from loja.modules.models import Product, ProductVariant
from loja.utils import utcnow

DEFAULT_RESERVATION_HOURS = 24
DEFAULT_RELEASE_LIMIT = 100
MAX_RESERVATION_HOURS = 24 * 30
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


def load_reservation_hours(value=None):
    if value is None:
        value = current_app.config.get(
            "STOCK_RESERVATION_HOURS",
            DEFAULT_RESERVATION_HOURS,
        )
    return _bounded_int(
        value,
        default=DEFAULT_RESERVATION_HOURS,
        minimum=1,
        maximum=MAX_RESERVATION_HOURS,
        code="stock_reservation_hours_invalid",
    )


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


def find_expired_stock_reservations(*, hours=None, limit=None, now=None):
    """Lista candidatos sem mutação para painel/observabilidade."""
    hours = load_reservation_hours(hours)
    limit = load_release_limit(limit)
    cutoff = (now or utcnow()) - timedelta(hours=hours)
    return (
        Order.query
        .filter(
            Order.stock_reserved.is_(True),
            Order.status == "pending_payment",
            Order.created_at < cutoff,
        )
        .order_by(Order.created_at.asc(), Order.id.asc())
        .limit(limit)
        .all()
    )


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


def release_expired_stock_reservations(
    *,
    hours=None,
    limit=None,
    source="service",
    now=None,
):
    """Libera reservas vencidas de forma idempotente e transacional.

    Uma consulta inicial obtém apenas IDs. Cada pedido é então relido com
    FOR UPDATE SKIP LOCKED e as condições são verificadas novamente sob o lock.
    Se o pagamento venceu a corrida, o status/flag mudou e a limpeza pula o
    pedido. Se a limpeza venceu, o webhook posterior verá stock_reserved=False
    e seguirá a regra normal de pagamento tardio.
    """
    hours = load_reservation_hours(hours)
    limit = load_release_limit(limit)
    cutoff = (now or utcnow()) - timedelta(hours=hours)
    source = str(source or "service")[:32]

    candidate_ids = [
        row[0]
        for row in (
            db.session.query(Order.id)
            .filter(
                Order.stock_reserved.is_(True),
                Order.status == "pending_payment",
                Order.created_at < cutoff,
            )
            .order_by(Order.created_at.asc(), Order.id.asc())
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
                .with_for_update(skip_locked=True)
                .first()
            )
            if (
                order is None
                or not order.stock_reserved
                or order.status != "pending_payment"
                or order.created_at is None
                or order.created_at >= cutoff
            ):
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
