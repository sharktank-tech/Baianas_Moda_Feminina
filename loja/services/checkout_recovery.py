"""FASE 9.0.0-C — recuperação de checkout/pedido pendente (somente e-mail).

Encontra pedidos abandonados em ``pending_payment`` dentro da janela em que o
estoque ainda está reservado e, gated por env ``CHECKOUT_RECOVERY_ENABLED == 1``
(default OFF), envia um lembrete único por pedido apontando para a área do
cliente. Nunca inicia tráfego pago, nunca chama provider e nunca reenvia o mesmo
pedido (dedupe via ``recovery_email_sent_at``).

Fail-closed: qualquer erro de envio marca o pedido como lembrado apenas se o
e-mail realmente foi despachado; caso contrário permanece NULL para a próxima
execução sem risco de spam duplicado.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, Optional

from loja.modules.enviar_email import enviar_email
from loja.modules.getway_mercado_pago.payment_models import Order
from loja.utils import utcnow


ENABLED_ENV = "CHECKOUT_RECOVERY_ENABLED"
AFTER_HOURS_ENV = "CHECKOUT_RECOVERY_AFTER_HOURS"
DEFAULT_AFTER_HOURS = 2
_MAX_AFTER_HOURS = 24 * 30
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
) -> list[Order]:
    """Pedidos pending_payment ainda com reserva, nunca antes lembrados.

    Idade mínima impede lembrete imediato após o checkout; a reserva expira
    via ``release_expired_stock_reservations`` e encerra a janela.
    """
    hours = after_hours if after_hours is not None else load_after_hours()
    if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= _MAX_AFTER_HOURS:
        raise ValueError("checkout_recovery_after_hours_invalid")
    now = now or utcnow()
    cutoff = now - timedelta(hours=hours)

    query = (
        Order.query
        .filter(
            Order.status == "pending_payment",
            Order.stock_reserved.is_(True),
            Order.recovery_email_sent_at.is_(None),
            Order.created_at < cutoff,
            Order.customer_email.isnot(None),
            Order.customer_email != "",
        )
        .order_by(Order.created_at.asc())
    )
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def _format_total(order) -> str:
    try:
        return f"{float(order.total):.2f}"
    except (TypeError, ValueError):
        return "—"


def _build_message(order, order_detail_url: str) -> str:
    """Corpo em texto simples (o remetente escapa antes de virar HTML)."""
    lines = [
        "Olá!",
        "",
        "Parece que você iniciou uma compra na Pollo Modas e o pagamento",
        f"ainda está pendente. Seus itens estão reservados por um tempo,",
        "mas podemos perder a reserva se o pagamento não for concluído.",
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
    """Despacha lembretes únicos por pedido (gated pelo env da aplicação).

    ``order_detail_url_builder`` recebe um Order e devolve a URL absoluta da
    área do cliente; default constrói via url_for(nome da rota, external).
    ``email_sender`` é injetável para testes (default: enviar_email).
    """
    enabled = is_recovery_enabled()
    if not enabled:
        return CheckoutRecoveryResult(
            enabled=False, candidates=0, sent=0,
            skipped_empty_email=0, failed=0,
        )

    if order_detail_url_builder is None:
        from flask import url_for

        def _default_builder(order) -> str:
            return url_for(
                _ORDER_DETAIL_URL_NAME,
                order_id=order.id,
                _external=True,
            )

        order_detail_url_builder = _default_builder

    sender = email_sender or enviar_email

    candidates = list(orders)
    sent = 0
    failed = 0
    skipped_empty_email = 0

    for order in candidates:
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
            # Não marca o pedido como lembrado: falha de envio não é lembrete.
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
    limit: Optional[int] = None,
    order_detail_url_builder=None,
    email_sender=None,
    after_hours: Optional[int] = None,
) -> CheckoutRecoveryResult:
    """Busca candidatos e envia lembretes; persiste dedupe somente se enviado."""
    enabled = is_recovery_enabled()
    if not enabled:
        return CheckoutRecoveryResult(
            enabled=False, candidates=0, sent=0,
            skipped_empty_email=0, failed=0,
        )

    orders = find_recoverable_orders(
        after_hours=after_hours,
        limit=limit,
    )
    result = send_recovery_emails(
        orders,
        order_detail_url_builder=order_detail_url_builder,
        email_sender=email_sender,
        after_hours=after_hours,
    )
    if result.sent > 0 or result.failed > 0 or result.skipped_empty_email > 0:
        from loja import db
        db.session.commit()
    return result


__all__ = [
    "CheckoutRecoveryResult",
    "find_recoverable_orders",
    "is_recovery_enabled",
    "load_after_hours",
    "run_checkout_recovery",
    "send_recovery_emails",
]