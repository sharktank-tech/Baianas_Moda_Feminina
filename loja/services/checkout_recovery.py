"""FASE 9.0.0-C/E — recuperação de checkout/pedido pendente por e-mail.

A execução é explicitamente gated e acontece somente pelo comando administrativo.
A seleção usa lock pessimista para impedir que workers concorrentes processem o
mesmo pedido. SMTP e banco não formam uma transação distribuída; portanto a
garantia é at-most-once entre execuções concorrentes e best-effort diante de
queda do processo entre envio e commit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, Optional

from loja.modules.enviar_email import email_delivery_ready, enviar_email
from loja.modules.getway_mercado_pago.payment_models import Order
from loja.utils import utcnow


ENABLED_ENV = "CHECKOUT_RECOVERY_ENABLED"
BATCH_ENABLED_ENV = "CHECKOUT_RECOVERY_BATCH_ENABLED"
AFTER_HOURS_ENV = "CHECKOUT_RECOVERY_AFTER_HOURS"
DEFAULT_AFTER_HOURS = 2
DEFAULT_RESERVATION_HOURS = 24
DEFAULT_BATCH_LIMIT = 50
_MAX_AFTER_HOURS = 24 * 30
_MAX_BATCH_LIMIT = 200
_ORDER_DETAIL_URL_NAME = "main.detalhes_encomenda"
_SUBJECT = "Você deixou um pedido para trás — Pollo Modas"


@dataclass(frozen=True)
class CheckoutRecoveryResult:
    enabled: bool
    candidates: int
    sent: int
    skipped_empty_email: int
    failed: int


def is_recovery_enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "") == "1"


def is_batch_recovery_enabled() -> bool:
    return os.environ.get(BATCH_ENABLED_ENV, "") == "1"


def load_after_hours() -> int:
    raw = os.environ.get(AFTER_HOURS_ENV)
    if raw is None or raw == "":
        return DEFAULT_AFTER_HOURS
    if raw != raw.strip() or not raw.isdigit():
        raise ValueError("checkout_recovery_after_hours_invalid")
    value = int(raw)
    if not 1 <= value <= _MAX_AFTER_HOURS:
        raise ValueError("checkout_recovery_after_hours_invalid")
    return value


def find_recoverable_orders(
    *,
    after_hours: Optional[int] = None,
    limit: Optional[int] = None,
    now=None,
    lock: bool = False,
    order_id=None,
) -> list[Order]:
    """Seleciona apenas pedidos cuja reserva de 24h ainda não expirou.

    lock=True usa FOR UPDATE SKIP LOCKED. A listagem administrativa usa
    leitura normal; a execução usa lock até o commit posterior ao envio.
    """
    hours = after_hours if after_hours is not None else load_after_hours()
    if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= _MAX_AFTER_HOURS:
        raise ValueError("checkout_recovery_after_hours_invalid")
    if limit is not None and (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= _MAX_BATCH_LIMIT
    ):
        raise ValueError("checkout_recovery_limit_invalid")

    now = now or utcnow()
    minimum_age = now - timedelta(hours=hours)
    reservation_started_after = now - timedelta(hours=DEFAULT_RESERVATION_HOURS)

    query = (
        Order.query
        .filter(
            Order.status == "pending_payment",
            Order.stock_reserved.is_(True),
            Order.recovery_email_sent_at.is_(None),
            Order.created_at < minimum_age,
            Order.created_at >= reservation_started_after,
            Order.customer_email.isnot(None),
            Order.customer_email != "",
        )
        .order_by(Order.created_at.asc())
    )
    if order_id is not None:
        query = query.filter(Order.id == order_id)
    if lock:
        query = query.with_for_update(of=Order, skip_locked=True)
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def _format_total(order) -> str:
    try:
        return f"{float(order.total):.2f}"
    except (TypeError, ValueError):
        return "—"


def _build_message(order, order_detail_url: str) -> str:
    lines = [
        "Olá!",
        "",
        "Parece que você iniciou uma compra na Pollo Modas e o pagamento",
        "ainda está pendente. Seus itens continuam reservados por um tempo,",
        "mas a reserva expira se o pagamento não for concluído.",
        "",
        "Para finalizar sua compra, acesse o passo a passo abaixo:",
        "",
        order_detail_url,
        "",
        f"Total do pedido: R$ {_format_total(order)}",
        "",
        "Se você já concluiu o pagamento, ignore esta mensagem.",
        "",
        "Pollo Modas",
    ]
    return "\n".join(lines).strip()


def send_recovery_emails(
    orders: Iterable[Order],
    *,
    order_detail_url_builder=None,
    email_sender=None,
    after_hours: Optional[int] = None,
) -> CheckoutRecoveryResult:
    """Envia a coleção já selecionada; o chamador mantém os locks até o commit."""
    if not is_recovery_enabled():
        return CheckoutRecoveryResult(False, 0, 0, 0, 0)

    if order_detail_url_builder is None:
        from flask import url_for

        def _default_builder(order) -> str:
            return url_for(
                _ORDER_DETAIL_URL_NAME,
                order_id=order.id,
                _external=True,
            )

        order_detail_url_builder = _default_builder

    if email_sender is None and not email_delivery_ready():
        raise ValueError("checkout_recovery_email_unavailable")

    sender = email_sender or enviar_email
    candidates = list(orders)
    sent = failed = skipped_empty_email = 0

    for order in candidates:
        if getattr(order, "recovery_email_sent_at", None) is not None:
            continue
        email = (getattr(order, "customer_email", None) or "").strip()
        if not email:
            skipped_empty_email += 1
            continue
        try:
            sender(
                email,
                _SUBJECT,
                _build_message(order, order_detail_url_builder(order)),
            )
            order.recovery_email_sent_at = utcnow()
            sent += 1
        except Exception:
            failed += 1

    return CheckoutRecoveryResult(
        enabled=True,
        candidates=len(candidates),
        sent=sent,
        skipped_empty_email=skipped_empty_email,
        failed=failed,
    )


def run_checkout_recovery(
    *,
    limit: Optional[int] = DEFAULT_BATCH_LIMIT,
    order_detail_url_builder=None,
    email_sender=None,
    after_hours: Optional[int] = None,
) -> CheckoutRecoveryResult:
    """Processa um lote limitado, fora dos webhooks e sob lock pessimista."""
    if not is_recovery_enabled() or not is_batch_recovery_enabled():
        return CheckoutRecoveryResult(False, 0, 0, 0, 0)

    from loja import db

    try:
        orders = find_recoverable_orders(
            after_hours=after_hours,
            limit=limit,
            lock=True,
        )
        result = send_recovery_emails(
            orders,
            order_detail_url_builder=order_detail_url_builder,
            email_sender=email_sender,
            after_hours=after_hours,
        )
        db.session.commit()
        return result
    except Exception:
        db.session.rollback()
        raise


def send_single_checkout_recovery(
    order_id,
    *,
    order_detail_url_builder=None,
    email_sender=None,
    after_hours: Optional[int] = None,
) -> CheckoutRecoveryResult:
    """Envia um único lembrete elegível, sob lock e com dedupe persistido."""
    if not is_recovery_enabled():
        return CheckoutRecoveryResult(False, 0, 0, 0, 0)
    if email_sender is None and not email_delivery_ready():
        raise ValueError("checkout_recovery_email_unavailable")

    from loja import db

    try:
        orders = find_recoverable_orders(
            after_hours=after_hours,
            limit=1,
            lock=True,
            order_id=order_id,
        )
        if not orders:
            return CheckoutRecoveryResult(True, 0, 0, 0, 0)
        result = send_recovery_emails(
            orders,
            order_detail_url_builder=order_detail_url_builder,
            email_sender=email_sender,
            after_hours=after_hours,
        )
        db.session.commit()
        return result
    except Exception:
        db.session.rollback()
        raise


__all__ = [
    "CheckoutRecoveryResult",
    "DEFAULT_AFTER_HOURS",
    "DEFAULT_BATCH_LIMIT",
    "DEFAULT_RESERVATION_HOURS",
    "find_recoverable_orders",
    "is_batch_recovery_enabled",
    "is_recovery_enabled",
    "load_after_hours",
    "run_checkout_recovery",
    "send_recovery_emails",
    "send_single_checkout_recovery",
]
