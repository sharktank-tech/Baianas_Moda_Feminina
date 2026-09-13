"""FASE 8.1.0 — hook pós-pagamento para auto-dispatch AliExpress.

Este módulo NÃO altera a transação financeira do Mercado Pago. O handler roda
somente em ``request_finished``, depois de a resposta ter sido construída e
apenas quando o terceiro gate explícito abaixo está ligado.

Gates necessários para uma submissão real:
1. ``ALIEXPRESS_POSTPAYMENT_AUTODISPATCH_ENABLED=1`` — habilita este hook;
2. ``ALIEXPRESS_PRODUCTION_FULFILLMENT_ENABLED=1`` — exigido pelo orquestrador;
3. ``ALIEXPRESS_PLACEORDER_ENABLED=1`` — exigido pelo provider real.

Qualquer exceção deste hook é absorvida e sanitizada: pagamento aprovado nunca
é revertido e o webhook do Mercado Pago nunca é transformado em retry por erro
do fornecedor. Não existe retry, loop de resend ou reconciliação implícita.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

from flask import current_app, has_request_context, request
from flask.signals import request_finished

from loja import db
from loja.modules.getway_mercado_pago.payment_models import Payment
from loja.modules.models import AliExpressOrder

from .production_orchestrator import (
    ProductionOrchestrationResult,
    orchestrate_production_submission,
)
from .submission_snapshot import (
    SubmissionSnapshotError,
    materialize_submission_snapshot,
)


POSTPAYMENT_AUTODISPATCH_ENABLED_ENV = "ALIEXPRESS_POSTPAYMENT_AUTODISPATCH_ENABLED"
POSTPAYMENT_DISABLED = "postpayment_autodispatch_disabled"
POSTPAYMENT_NOT_PAID = "postpayment_order_not_paid"
POSTPAYMENT_NO_FULFILLMENT = "postpayment_no_aliexpress_fulfillment"
POSTPAYMENT_SNAPSHOT_FAILED = "postpayment_snapshot_failed"
POSTPAYMENT_INTERNAL_ERROR = "postpayment_autodispatch_error"

_PAYMENT_ENDPOINTS = frozenset(
    {
        "payments.mercado_pago_webhook",
        "payments.payment_return",
    }
)


@dataclass(frozen=True)
class PostpaymentAutodispatchResult:
    enabled: bool
    order_count: int
    snapshot_count: int
    dispatch_count: int
    code: Optional[str]


def postpayment_autodispatch_enabled() -> bool:
    """Fail-closed: somente a string exata ``1`` habilita o hook."""
    return os.environ.get(POSTPAYMENT_AUTODISPATCH_ENABLED_ENV) == "1"


def _result(
    *,
    enabled: bool,
    order_count: int = 0,
    snapshot_count: int = 0,
    dispatch_count: int = 0,
    code: Optional[str] = None,
) -> PostpaymentAutodispatchResult:
    return PostpaymentAutodispatchResult(
        enabled=enabled,
        order_count=order_count,
        snapshot_count=snapshot_count,
        dispatch_count=dispatch_count,
        code=code,
    )


def dispatch_paid_order_after_payment(
    order,
    *,
    session=None,
    materialize_fn: Callable = materialize_submission_snapshot,
    orchestrate_fn: Callable[..., ProductionOrchestrationResult] = (
        orchestrate_production_submission
    ),
) -> PostpaymentAutodispatchResult:
    """Materializa snapshots locais e orquestra cada fulfillment no máximo 1x.

    A função exige o terceiro gate antes de QUALQUER efeito. Só processa pedidos
    persistidos como ``paid``. Cada AliExpressOrder ``ready`` pode receber no
    máximo uma chamada ao orquestrador nesta execução. Estados avançados não
    são rematerializados nem re-disparados.
    """
    if not postpayment_autodispatch_enabled():
        return _result(enabled=False, code=POSTPAYMENT_DISABLED)

    session = session or db.session
    if order is None or getattr(order, "status", None) != "paid":
        return _result(enabled=True, code=POSTPAYMENT_NOT_PAID)

    ali_orders = (
        session.query(AliExpressOrder)
        .filter_by(order_id=order.id)
        .order_by(AliExpressOrder.created_at.asc(), AliExpressOrder.id.asc())
        .all()
    )
    if not ali_orders:
        return _result(enabled=True, code=POSTPAYMENT_NO_FULFILLMENT)

    snapshot_count = 0
    dispatch_count = 0
    ready_count = 0

    for ali_order in ali_orders:
        if ali_order.state != "ready":
            continue
        ready_count += 1

        try:
            snap_result = materialize_fn(ali_order, session=session)
            # O snapshot precisa estar durável ANTES do orquestrador. Se o
            # processo morrer depois deste commit, uma reentrega apenas o reusa.
            session.commit()
            if getattr(snap_result, "snapshot", None) is not None:
                snapshot_count += 1
        except SubmissionSnapshotError:
            session.rollback()
            current_app.logger.warning(
                "postpayment autodispatch bloqueado code=%s order=%s",
                POSTPAYMENT_SNAPSHOT_FAILED,
                order.id,
            )
            continue
        except Exception:
            session.rollback()
            current_app.logger.error(
                "postpayment autodispatch erro local code=%s order=%s",
                POSTPAYMENT_INTERNAL_ERROR,
                order.id,
            )
            continue

        try:
            orchestration = orchestrate_fn(ali_order, session=session)
            if getattr(orchestration, "dispatched", False):
                dispatch_count += 1
        except Exception:
            # Não propaga erro do fornecedor/provider ao fluxo financeiro.
            session.rollback()
            current_app.logger.error(
                "postpayment autodispatch erro de orquestracao code=%s order=%s",
                POSTPAYMENT_INTERNAL_ERROR,
                order.id,
            )

    if ready_count == 0:
        return _result(
            enabled=True,
            order_count=len(ali_orders),
            code=POSTPAYMENT_NO_FULFILLMENT,
        )

    return _result(
        enabled=True,
        order_count=len(ali_orders),
        snapshot_count=snapshot_count,
        dispatch_count=dispatch_count,
        code=None,
    )


def _normalized_payment_id(value) -> Optional[str]:
    normalized = str(value or "").strip()
    if not normalized or normalized.lower() in {"null", "none", "undefined"}:
        return None
    return normalized


def _payment_id_from_request() -> Optional[str]:
    endpoint = request.endpoint
    if endpoint == "payments.mercado_pago_webhook":
        body = request.get_json(silent=True) or {}
        return _normalized_payment_id(
            request.args.get("data.id")
            or (body.get("data") or {}).get("id")
            or request.args.get("id")
        )
    if endpoint == "payments.payment_return":
        return _normalized_payment_id(
            request.args.get("payment_id") or request.args.get("collection_id")
        )
    return None


def _run_postpayment_autodispatch(sender, response, **extra) -> None:
    """Signal handler best-effort; jamais altera a resposta já construída."""
    if not postpayment_autodispatch_enabled():
        return
    if not has_request_context():
        return
    if request.endpoint not in _PAYMENT_ENDPOINTS:
        return
    if response is None or int(getattr(response, "status_code", 500) or 500) >= 400:
        return

    payment_id = _payment_id_from_request()
    if not payment_id:
        return

    try:
        payment = Payment.query.filter_by(provider_payment_id=payment_id).first()
        if payment is None or payment.order is None or payment.order.status != "paid":
            return
        dispatch_paid_order_after_payment(payment.order)
    except Exception:
        db.session.rollback()
        current_app.logger.error(
            "postpayment autodispatch signal falhou code=%s",
            POSTPAYMENT_INTERNAL_ERROR,
        )


# Registro único por import de módulo. ``weak=False`` evita coleta do receiver.
request_finished.connect(_run_postpayment_autodispatch, weak=False)


__all__ = [
    "POSTPAYMENT_AUTODISPATCH_ENABLED_ENV",
    "POSTPAYMENT_DISABLED",
    "POSTPAYMENT_NOT_PAID",
    "POSTPAYMENT_NO_FULFILLMENT",
    "POSTPAYMENT_SNAPSHOT_FAILED",
    "POSTPAYMENT_INTERNAL_ERROR",
    "PostpaymentAutodispatchResult",
    "postpayment_autodispatch_enabled",
    "dispatch_paid_order_after_payment",
]
