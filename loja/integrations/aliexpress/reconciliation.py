"""Reconciliação de submissão AliExpress (Fase 8.0.5/8.0.11H) — HARD-OFF.

Elimina a ambiguidade do estado ``submitting -> provider possivelmente aceitou
-> processo falha antes do commit local``. A ordem em resultado ambíguo vai
para ``pending_reconciliation`` e NÃO volta a ``ready`` nem permite nova
criação: exige reconciliação explícita por este boundary.

Fase 8.0.11H adiciona uma regra operacional importante: um desfecho conclusivo
da reconciliação também encerra a barreira durável da tentativa. ``found``
marca a tentativa como ``confirmed``; ``not_found`` marca como ``rejected``;
``unknown``/erro permanece ``unknown`` e continua bloqueando o circuit breaker
global do canário. Não existe liberação por idade.

SEPARAÇÃO (8.0.4/8.0.5):
    submission boundary -> pedido (AliExpressOrderSubmitter)
    OAuth boundary      -> credencial (AliExpressOAuthRefresher)
    reconciliation      -> status remoto (AliExpressOrderReconciler)
    Nenhuma mistura: reconciliação NÃO usa OAuth/credencial nem re-consulta o
    catálogo; trabalha apenas sobre o snapshot persistido.

RESPOSTAS SEMÂNTICAS DO RECONCILER:
    * found      -> pedido remoto existe (persiste ali_order_id; submitted)
    * not_found  -> conclusivo: NENHUM pedido foi criado (retry seguro na MESMA
                    AliExpressOrder, snapshot preservado, sem nova ordem)
    * unknown    -> estado ainda desconhecido (permanece bloqueado p/ criação;
                    nunca retry cego)
    * erro       -> exceção de reconciliação (permanece bloqueado; erro
                    sanitizado em last_error)

Nesta fase o provider de reconciliação é sempre HARD-OFF (fake/mock nos
testes); nenhuma consulta ao AliExpress real acontece.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from loja import db
from loja.modules.models import (
    AliExpressOrder,
    AliExpressOrderItem,
    AliExpressSubmissionAttempt,
)
from loja.utils import utcnow

from .fulfillment import ERROR_CODES, FulfillmentResult
from .submission import _attempt_key

logger = logging.getLogger(__name__)


class AliExpressReconciliationError(Exception):
    """Erro de reconciliação — nunca contém tokens/secrets."""


class AliExpressReconciliationUnavailable(AliExpressReconciliationError):
    """Reconciliação indisponível (provider padrão HARD-OFF 8.0.5)."""


@dataclass(frozen=True)
class AliExpressOrderReconciliationRequest:
    """Dados mínimos para reconciliar — fonte exclusiva: snapshot persistido.

    Nunca contém credencial, token, segredo ou payload OAuth.
    """

    aliexpress_order_id: uuid.UUID
    order_id: Optional[uuid.UUID]
    supplier_id: Optional[uuid.UUID]
    attempt_key: Optional[str]


#: Desfechos semânticos da reconciliação (string estáveis, sem secrets).
RECONCILE_FOUND = "found"
RECONCILE_NOT_FOUND = "not_found"
RECONCILE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class AliExpressOrderReconciliationResult:
    """Resposta do reconciler (fake) — sem secrets."""

    outcome: str  # RECONCILE_FOUND | RECONCILE_NOT_FOUND | RECONCILE_UNKNOWN
    external_order_id: Optional[str] = None
    external_status: Optional[str] = None


class AliExpressOrderReconciler(ABC):
    """Boundary de reconciliação: o domínio nunca fala HTTP diretamente."""

    @abstractmethod
    def reconcile(
        self, request: AliExpressOrderReconciliationRequest
    ) -> AliExpressOrderReconciliationResult:
        """Responde se o pedido remoto existe (found/not_found), se ainda é
        desconhecido (unknown) ou levanta ``AliExpressReconciliationError``."""


class HardOffOrderReconciler(AliExpressOrderReconciler):
    """Implementação ativa: NUNCA executa rede, falha determinística."""

    def reconcile(
        self, request: AliExpressOrderReconciliationRequest
    ) -> AliExpressOrderReconciliationResult:
        raise AliExpressReconciliationUnavailable(
            "reconciliação AliExpress desabilitada (HARD-OFF 8.0.5): "
            "nenhuma consulta externa é realizada"
        )


def get_default_order_reconciler() -> AliExpressOrderReconciler:
    """Provider padrão de produção/dev — sempre hard-off, sem config ambígua."""
    return HardOffOrderReconciler()


def _integrity_gate(items: list[AliExpressOrderItem]) -> Optional[str]:
    """Integridade de snapshot tem precedência sobre reconciliação."""
    if not items:
        return ERROR_CODES["ITEM_INTEGRITY_ERROR"]
    for item in items:
        if (
            item is None
            or item.state == "blocked"
            or item.last_error == ERROR_CODES["ITEM_INTEGRITY_ERROR"]
            or not item.supplier_product_external_id
            or not item.supplier_sku_external_id
        ):
            return ERROR_CODES["ITEM_INTEGRITY_ERROR"]
    return None


def _resolve_attempt(order, *, state: str, now, session) -> None:
    """Resolve a tentativa durável atual sem expor sua identidade.

    A tentativa é localizada por ``order.id + order.attempt_key``. Ausência é
    tolerada para compatibilidade com pedidos legados; quando existe, o estado
    terminal é persistido na MESMA transação da reconciliação conclusiva.
    """
    if not order.attempt_key:
        return
    attempt = (
        session.query(AliExpressSubmissionAttempt)
        .filter_by(
            aliexpress_order_id=order.id,
            attempt_key=order.attempt_key,
        )
        .first()
    )
    if attempt is None:
        return
    attempt.state = state
    attempt.completed_at = now
    attempt.last_error = None
    attempt.updated_at = now


def reconcile_aliexpress_order(
    ali_order: AliExpressOrder,
    reconciler: Optional[AliExpressOrderReconciler] = None,
    session=None,
) -> FulfillmentResult:
    """Reconcilia uma AliExpressOrder em ``pending_reconciliation``.

    Locking (PostgreSQL): ``SELECT ... FOR UPDATE`` + ``populate_existing()``
    serializa a reconciliação da MESMA ordem — duas reconciliações concorrentes
    não divergem e DEPOIS da primeira (found/not_found) a segunda re-lê o novo
    estado e não chama o reconciler de novo.

    Regras:
      * ``submitted`` permanece idempotente (retorna submitted);
      * apenas ``pending_reconciliation`` é reconciliável;
      * ``item_integrity_error`` tem precedência: nunca chama o reconciler;
      * ``found``  -> persiste ``ali_order_id``, termina ``submitted``, marca a
                      tentativa durável como ``confirmed`` e NÃO chama o
                      submitter novamente;
      * ``not_found`` (conclusivo) -> volta a ``ready`` na MESMA AliExpressOrder,
                      marca a tentativa como ``rejected`` e permite uma nova
                      tentativa com nova attempt_key;
      * ``unknown``/erro -> permanece ``pending_reconciliation`` e a tentativa
                      permanece ``unknown``; nunca retry cego.

    Em SQLite o FOR UPDATE é inócuo; a exclusão mútua é provada em PostgreSQL.
    """
    if reconciler is None:
        reconciler = get_default_order_reconciler()
    session = session or db.session
    now = utcnow()

    order = (
        session.query(AliExpressOrder)
        .filter_by(id=ali_order.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if order is None:
        raise ValueError(f"AliExpressOrder {ali_order.id} não encontrada")

    if order.state == "submitted":
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state="submitted",
        )

    if order.state != "pending_reconciliation":
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state=order.state,
        )

    items = list(order.items)
    reason = _integrity_gate(items)
    if reason is not None:
        order.last_error = reason
        order.updated_at = now
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state="blocked",
        )

    request = AliExpressOrderReconciliationRequest(
        aliexpress_order_id=order.id,
        order_id=order.order_id,
        supplier_id=order.supplier_id,
        attempt_key=order.attempt_key or _attempt_key(order),
    )

    try:
        result = reconciler.reconcile(request)
    except AliExpressReconciliationUnavailable:
        logger.warning(
            "aliexpress reconciliation: indisponível p/ %s code=%s",
            order.id, ERROR_CODES["RECONCILIATION_ERROR"],
        )
        order.last_error = ERROR_CODES["RECONCILIATION_ERROR"]
        order.updated_at = now
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state=order.state,
        )
    except AliExpressReconciliationError:
        logger.warning(
            "aliexpress reconciliation: erro p/ %s code=%s",
            order.id, ERROR_CODES["RECONCILIATION_ERROR"],
        )
        order.last_error = ERROR_CODES["RECONCILIATION_ERROR"]
        order.updated_at = now
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state=order.state,
        )
    except Exception:
        logger.error(
            "aliexpress reconciliation: erro interno p/ %s code=%s",
            order.id, ERROR_CODES["RECONCILIATION_ERROR"],
        )
        order.last_error = ERROR_CODES["RECONCILIATION_ERROR"]
        order.updated_at = now
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state=order.state,
        )

    if result is None:
        order.last_error = ERROR_CODES["RECONCILIATION_ERROR"]
        order.updated_at = now
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state=order.state,
        )

    outcome = getattr(result, "outcome", "")
    if outcome == RECONCILE_FOUND:
        external_order_id = getattr(result, "external_order_id", None) or ""
        if not external_order_id:
            order.last_error = ERROR_CODES["RECONCILIATION_ERROR"]
            order.updated_at = now
            session.commit()
            return FulfillmentResult(
                aliexpress_order=order,
                items=list(order.items),
                state=order.state,
            )
        order.state = "submitted"
        order.ali_order_id = external_order_id
        order.ali_status = getattr(result, "external_status", None) or "submitted"
        order.submitted_at = now
        order.last_error = None
        order.updated_at = now
        for item in items:
            item.state = "submitted"
            item.last_error = None
            item.updated_at = now
        _resolve_attempt(order, state="confirmed", now=now, session=session)
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state="submitted",
        )

    if outcome == RECONCILE_NOT_FOUND:
        # Conclusivo: nenhum pedido criado remotamente. Retry seguro na MESMA
        # ordem (snapshot preservado; nova tentativa gera nova attempt_key).
        order.state = "ready"
        order.last_error = None
        order.updated_at = now
        _resolve_attempt(order, state="rejected", now=now, session=session)
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state="ready",
        )

    order.last_error = ERROR_CODES["RECONCILIATION_UNKNOWN"]
    order.updated_at = now
    session.commit()
    return FulfillmentResult(
        aliexpress_order=order,
        items=list(order.items),
        state="pending_reconciliation",
    )


__all__ = [
    "AliExpressOrderReconciler",
    "AliExpressOrderReconciliationRequest",
    "AliExpressOrderReconciliationResult",
    "HardOffOrderReconciler",
    "get_default_order_reconciler",
    "reconcile_aliexpress_order",
    "AliExpressReconciliationError",
    "AliExpressReconciliationUnavailable",
    "RECONCILE_FOUND",
    "RECONCILE_NOT_FOUND",
    "RECONCILE_UNKNOWN",
]
