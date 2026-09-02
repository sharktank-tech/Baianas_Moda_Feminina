"""Sincronização de status pós-submissão AliExpress (Fase 8.0.8) — GATED.

Localiza o ciclo de vida do fornecedor a partir do estado remoto de uma
AliExpressOrder que JÁ possui identidade externa confiável (``ali_order_id``).

RESPONSABILIDADE ÚNICA:
    consultar semanticamente o estado de um AliExpressOrder já identificado
    remotamente e AVANÇAR monotonicamente a máquina local:

        submitted -> confirmed -> shipped -> delivered

    e aceitar observação remota conclusiva de cancelamento:

        submitted/confirmed/shipped -> cancelled     (cancelled_at = now)

    O domínio nunca fala HTTP diretamente via este módulo. O provider padrão
    é fail-closed e permanece ``HardOffOrderStatusSyncer`` salvo quando a flag
    explícita abaixo estiver EXATAMENTE habilitada:

        ALIEXPRESS_ORDER_STATUS_SYNC_ENABLED=1

    Só nesse caso ``get_default_order_status_syncer()`` constrói o adapter DS
    read-only provado na Fase 8.0.7B (``aliexpress.trade.ds.order.get``).
    Ausência da flag ou qualquer outro valor mantém ZERO HTTP por default.

MÁQUINA MONOTÔNICA:
    Ordem canônica: submitted(1) < confirmed(2) < shipped(3) < delivered(4).
    * resultado igual ou POSTERIOR  -> avança/persiste milestones;
    * resultado ANTERIOR ao local   -> IGNORADO, sem regressão, sem reescrita
      de timestamps antigos;
    * mesmo estado                  -> idempotente (nunca sobrescreve timestamp
      já existente);
    * ``delivered`` é terminal: NADA regride uma ordem entregue.

ELEGIBILIDADE:
    Só consulta provider quem tem:
      * ``AliExpressOrder.ali_order_id`` preenchido;
      * state em submitted/confirmed/shipped/delivered.
    ``pending_reconciliation`` NÃO é resolvida aqui.

SEGURANÇA:
    Nunca persiste em ``last_error`` nem loga: exception bruta, body HTTP,
    token, secret, header ou payload provider. ``external_status`` é dado não
    confiável e nunca é logado sem sanitização. ``ali_status`` só recebe
    valores canônicos (outcome) — nunca texto bruto arbitrário do provider.
"""

from __future__ import annotations

import logging
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from loja import db
from loja.modules.models import AliExpressOrder
from loja.utils import utcnow

from .fulfillment import ERROR_CODES, FulfillmentResult

logger = logging.getLogger(__name__)

STATUS_SYNC_ENABLE_ENV = "ALIEXPRESS_ORDER_STATUS_SYNC_ENABLED"

#: Desfechos semânticos do syncer (valores canônicos estáveis, sem secrets).
STATUS_SUBMITTED = "submitted"
STATUS_CONFIRMED = "confirmed"
STATUS_SHIPPED = "shipped"
STATUS_DELIVERED = "delivered"
STATUS_CANCELLED = "cancelled"
STATUS_UNKNOWN = "unknown"

#: Ordem monotônica da máquina logística (entregue é terminal).
_MONO_ORDER = {
    STATUS_SUBMITTED: 1,
    STATUS_CONFIRMED: 2,
    STATUS_SHIPPED: 3,
    STATUS_DELIVERED: 4,
}

#: Estados com identidade externa confiável e elegíveis para o provider.
_ELIGIBLE_STATES = frozenset(
    {STATUS_SUBMITTED, STATUS_CONFIRMED, STATUS_SHIPPED, STATUS_DELIVERED}
)


class AliExpressOrderStatusSyncError(Exception):
    """Erro de sincronização de status — nunca contém tokens/secrets."""


class AliExpressOrderStatusSyncUnavailable(AliExpressOrderStatusSyncError):
    """Sincronização indisponível (provider padrão hard-off)."""


@dataclass(frozen=True)
class AliExpressOrderStatusRequest:
    """Dados mínimos para consultar status remoto — fonte: snapshot persistido.

    Nunca contém credencial, token, segredo ou payload OAuth.
    """

    aliexpress_order_id: uuid.UUID
    order_id: Optional[uuid.UUID]
    supplier_id: Optional[uuid.UUID]
    ali_order_id: Optional[str]
    attempt_key: Optional[str]


@dataclass(frozen=True)
class AliExpressOrderStatusResult:
    """Resposta do syncer — sem payload bruto, sem secrets.

    ``outcome`` é canônico (STATUS_*). ``external_status`` é dado NÃO
    confiável do provider: usado apenas se sanitizado e nunca logado.
    """

    outcome: str
    external_status: Optional[str] = None


class AliExpressOrderStatusSyncer(ABC):
    """Boundary de status: o domínio nunca fala HTTP diretamente."""

    @abstractmethod
    def sync(
        self, request: AliExpressOrderStatusRequest
    ) -> AliExpressOrderStatusResult:
        """Responde o status remoto semântico (STATUS_*) ou levanta
        ``AliExpressOrderStatusSyncError``."""


class HardOffOrderStatusSyncer(AliExpressOrderStatusSyncer):
    """Implementação fail-closed: NUNCA executa rede."""

    def sync(
        self, request: AliExpressOrderStatusRequest
    ) -> AliExpressOrderStatusResult:
        raise AliExpressOrderStatusSyncUnavailable(
            "sincronização de status AliExpress desabilitada (HARD-OFF): "
            "nenhuma consulta externa é realizada"
        )


def is_real_order_status_sync_enabled() -> bool:
    """Opt-in estrito: somente o valor literal ``1`` habilita HTTP real."""
    return os.environ.get(STATUS_SYNC_ENABLE_ENV) == "1"


def get_default_order_status_syncer() -> AliExpressOrderStatusSyncer:
    """Retorna provider real somente sob opt-in explícito; default hard-off.

    O import local evita ciclo de import: ``ds_order_status`` depende dos tipos
    definidos neste módulo. Construir o adapter ainda NÃO faz HTTP; rede só
    ocorre posteriormente, dentro de ``sync()``, e somente após as guards de
    elegibilidade de ``sync_aliexpress_order_status``.
    """
    if not is_real_order_status_sync_enabled():
        return HardOffOrderStatusSyncer()

    from .ds_order_status import AliExpressDSOrderStatusSyncer

    return AliExpressDSOrderStatusSyncer()


def _fill_milestones(order: AliExpressOrder, target_state: str, now):
    """Preenche timestamps dos milestones logísticos ALCANÇADOS pelo alvo."""
    target_rank = _MONO_ORDER.get(target_state, _MONO_ORDER[STATUS_SUBMITTED])
    if target_rank >= _MONO_ORDER[STATUS_CONFIRMED] and order.confirmed_at is None:
        order.confirmed_at = now
    if target_rank >= _MONO_ORDER[STATUS_SHIPPED] and order.shipped_at is None:
        order.shipped_at = now
    if target_rank >= _MONO_ORDER[STATUS_DELIVERED] and order.delivered_at is None:
        order.delivered_at = now


def sync_aliexpress_order_status(
    ali_order: AliExpressOrder,
    syncer: Optional[AliExpressOrderStatusSyncer] = None,
    session=None,
) -> FulfillmentResult:
    """Sincroniza status de uma AliExpressOrder com identidade remota confiável.

    Locking (PostgreSQL): ``SELECT ... FOR UPDATE`` + ``populate_existing()``
    serializa syncs concorrentes da MESMA ordem.

    Regras:
      * ``cancelled`` é terminal: idempotente sem provider;
      * sem ``ali_order_id`` OU estado inelegível -> NUNCA chama provider;
      * ``pending_reconciliation`` NÃO é resolvida aqui;
      * avanço confirmed/shipped/delivered preenche milestones;
      * regressão remota é IGNORADA;
      * delivered é terminal;
      * erro/unknown nunca regride o estado atual.
    """
    if syncer is None:
        syncer = get_default_order_status_syncer()
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

    if order.state == STATUS_CANCELLED:
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state=STATUS_CANCELLED,
        )

    if not order.ali_order_id or order.state not in _ELIGIBLE_STATES:
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state=order.state,
        )

    request = AliExpressOrderStatusRequest(
        aliexpress_order_id=order.id,
        order_id=order.order_id,
        supplier_id=order.supplier_id,
        ali_order_id=order.ali_order_id,
        attempt_key=order.attempt_key,
    )

    try:
        result = syncer.sync(request)
    except AliExpressOrderStatusSyncUnavailable:
        logger.warning(
            "aliexpress status sync: indisponível p/ %s code=%s",
            order.id, ERROR_CODES["STATUS_SYNC_HARD_OFF"],
        )
        order.last_error = ERROR_CODES["STATUS_SYNC_HARD_OFF"]
        order.updated_at = now
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state=order.state,
        )
    except AliExpressOrderStatusSyncError:
        logger.warning(
            "aliexpress status sync: falha p/ %s code=%s",
            order.id, ERROR_CODES["STATUS_SYNC_FAILED"],
        )
        order.last_error = ERROR_CODES["STATUS_SYNC_FAILED"]
        order.updated_at = now
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state=order.state,
        )
    except Exception:
        logger.error(
            "aliexpress status sync: erro interno p/ %s code=%s",
            order.id, ERROR_CODES["STATUS_SYNC_ERROR"],
        )
        order.last_error = ERROR_CODES["STATUS_SYNC_ERROR"]
        order.updated_at = now
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state=order.state,
        )

    if result is None or not getattr(result, "outcome", ""):
        order.last_error = ERROR_CODES["STATUS_SYNC_UNKNOWN"]
        order.updated_at = now
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state=order.state,
        )

    outcome = getattr(result, "outcome")
    if outcome == STATUS_CANCELLED:
        if order.state == STATUS_DELIVERED:
            order.last_error = ERROR_CODES["STATUS_SYNC_INVALID_TRANSITION"]
            order.updated_at = now
            session.commit()
            return FulfillmentResult(
                aliexpress_order=order,
                items=list(order.items),
                state=order.state,
            )
        order.state = STATUS_CANCELLED
        order.cancelled_at = now
        order.ali_status = STATUS_CANCELLED
        order.last_error = None
        order.updated_at = now
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state=STATUS_CANCELLED,
        )

    if outcome == STATUS_UNKNOWN or outcome not in _MONO_ORDER:
        order.last_error = ERROR_CODES["STATUS_SYNC_UNKNOWN"]
        order.updated_at = now
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state=order.state,
        )

    target_rank = _MONO_ORDER[outcome]
    local_rank = _MONO_ORDER[order.state]

    if outcome == order.state:
        _fill_milestones(order, order.state, now)
        order.ali_status = outcome
        order.last_error = None
        order.updated_at = now
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state=order.state,
        )

    if target_rank < local_rank:
        order.updated_at = now
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state=order.state,
        )

    order.state = outcome
    _fill_milestones(order, outcome, now)
    order.ali_status = outcome
    order.last_error = None
    order.updated_at = now
    session.commit()
    return FulfillmentResult(
        aliexpress_order=order,
        items=list(order.items),
        state=outcome,
    )


__all__ = [
    "STATUS_SYNC_ENABLE_ENV",
    "is_real_order_status_sync_enabled",
    "AliExpressOrderStatusSyncer",
    "AliExpressOrderStatusRequest",
    "AliExpressOrderStatusResult",
    "HardOffOrderStatusSyncer",
    "get_default_order_status_syncer",
    "sync_aliexpress_order_status",
    "AliExpressOrderStatusSyncError",
    "AliExpressOrderStatusSyncUnavailable",
    "STATUS_SUBMITTED",
    "STATUS_CONFIRMED",
    "STATUS_SHIPPED",
    "STATUS_DELIVERED",
    "STATUS_CANCELLED",
    "STATUS_UNKNOWN",
]
