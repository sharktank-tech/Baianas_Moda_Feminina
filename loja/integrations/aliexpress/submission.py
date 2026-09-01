"""Pipeline de submissão AliExpress (Fase 8.0.3) — HARD-OFF.

O fulfillment não executa HTTP diretamente. A submissão passa por um
boundary explícito ``AliExpressOrderSubmitter``; a implementação ativa em
produção/dev é ``HardOffOrderSubmitter``, que falha de forma determinística
(nenhuma chamada de rede é feita). Sucesso real só ocorre com um provider
fake/mock injetado nos testes.

CONTRATO DO SNAPSHOT:
    O payload de submissão é construído EXCLUSIVAMENTE a partir do snapshot
    persistido (``AliExpressOrder`` + ``AliExpressOrderItem``). O catálogo não
    participa após a materialização do snapshot — mesmo que SKU, custo,
    associação de variante ou o próprio produto mudem ou sejam removidos, o
    que é enviado é o histórico congelado em ``aliexpress_order_items``.

MÁQUINA DE ESTADOS:
    ready -> submitting -> submitted

    * ``submitting`` é persistido ANTES da primeira tentativa no provider;
    * ``submitted`` só existe após sucesso confirmado (identidade externa
      persistida em ``AliExpressOrder.ali_order_id``);
    * nunca há transição direta ``ready -> submitted``;
    * falha preserva a ordem/itens/id/snapshot e mantém ``submitting`` com
      ``last_error`` sanitizado e ``attempt_count + 1`` para retry síncrono.

IDEMPOTÊNCIA:
    Ordens já ``submitted`` retornam o resultado persistido sem chamar o
    provider nem gerar nova identidade externa.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from loja import db
from loja.modules.models import AliExpressOrder, AliExpressOrderItem
from loja.utils import utcnow

from .fulfillment import ERROR_CODES, FulfillmentResult, _check_credential

logger = logging.getLogger(__name__)


class SubmissionHardOffError(Exception):
    """Criação real de pedido AliExpress está desabilitada (HARD-OFF 8.0.3)."""


class AliExpressSubmissionFailed(Exception):
    """Falha do provider (fake) na submissão."""


@dataclass(frozen=True)
class AliExpressSubmissionItem:
    """Item de submissão congelado a partir do snapshot persistido."""

    order_item_id: uuid.UUID
    supplier_product_external_id: str
    supplier_sku_external_id: str
    quantity: int
    supplier_unit_cost: Optional[Decimal]
    supplier_currency: Optional[str]
    variant_label: Optional[str]


@dataclass(frozen=True)
class AliExpressSubmissionRequest:
    """Payload de submissão — fonte exclusiva: snapshot persistido."""

    aliexpress_order_id: uuid.UUID
    order_id: Optional[uuid.UUID]
    supplier_id: Optional[uuid.UUID]
    items: tuple[AliExpressSubmissionItem, ...]


@dataclass(frozen=True)
class AliExpressSubmissionResult:
    """Resposta do provider (fake) — identidade externa confirmada."""

    external_order_id: str
    external_status: str = "submitted"


class AliExpressOrderSubmitter(ABC):
    """Boundary de submissão: o domínio nunca fala HTTP diretamente."""

    @abstractmethod
    def submit(self, request: AliExpressSubmissionRequest) -> AliExpressSubmissionResult:
        """Envia o pedido. Deve levantar ``AliExpressSubmissionFailed`` em falha."""


class HardOffOrderSubmitter(AliExpressOrderSubmitter):
    """Implementação ativa: NUNCA executa rede, falha determinística."""

    def submit(self, request: AliExpressSubmissionRequest) -> AliExpressSubmissionResult:
        raise SubmissionHardOffError(
            "Criação real de pedido AliExpress desabilitada (HARD-OFF 8.0.3): "
            "nenhuma chamada externa é realizada."
        )


def get_default_order_submitter() -> AliExpressOrderSubmitter:
    """Provider padrão de produção/dev — sempre hard-off, sem config ambígua."""
    return HardOffOrderSubmitter()


def _build_submission_request(
    order: AliExpressOrder,
    items: list[AliExpressOrderItem],
) -> AliExpressSubmissionRequest:
    return AliExpressSubmissionRequest(
        aliexpress_order_id=order.id,
        order_id=order.order_id,
        supplier_id=order.supplier_id,
        items=tuple(
            AliExpressSubmissionItem(
                order_item_id=item.order_item_id,
                supplier_product_external_id=item.supplier_product_external_id,
                supplier_sku_external_id=item.supplier_sku_external_id,
                quantity=item.quantity,
                supplier_unit_cost=item.supplier_unit_cost,
                supplier_currency=item.supplier_currency,
                variant_label=item.variant_label,
            )
            for item in items
        ),
    )


def _submission_gate(
    order: AliExpressOrder,
    items: list[AliExpressOrderItem],
) -> Optional[str]:
    """Barreiras que impedem a submissão (nunca chegam ao provider).

    Credencial NUNCA supera erro de integridade de item: itens inválidos
    bloqueiam antes da checagem de auth. Retorna None se a ordem pode ser
    enviada ao provider.
    """
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

    if order.supplier_id is None:
        return ERROR_CODES["SUBMISSION_REQUIRES_SUPPLIER"]

    return _check_credential(order.supplier_id)


def _fail(
    *,
    order: AliExpressOrder,
    error: str,
    session,
) -> FulfillmentResult:
    """Persiste uma falha de submissão preservando a ordem e o snapshot."""
    order.last_error = error
    order.attempt_count += 1
    order.last_attempt_at = utcnow()
    order.updated_at = utcnow()
    session.commit()
    return FulfillmentResult(
        aliexpress_order=order,
        items=list(order.items),
        state=order.state,
    )


def submit_aliexpress_order(
    ali_order: AliExpressOrder,
    submitter: Optional[AliExpressOrderSubmitter] = None,
    session=None,
) -> FulfillmentResult:
    """Submete uma AliExpressOrder via máquina de estados idempotente.

    Locking (PostgreSQL): usa ``SELECT ... FOR UPDATE`` em dois estágios para
    serializar a chamada ao provider e impedir dupla submissão externa:
       Estágio A: trava, checa estado/gate, transiciona ``ready -> submitting``
                  e commit (submitting persistido ANTES do provider).
       Estágio B: re-trava, re-checa ``submitted`` (idempotência sob corrida),
                  chama o provider com o lock segurado e commit do resultado.

    Em SQLite o FOR UPDATE é inócuo; a proteção de concorrência é garantida e
    testada em PostgreSQL.
    """
    if submitter is None:
        submitter = get_default_order_submitter()
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

    if order.state not in ("ready", "submitting"):
        # Estados não elegíveis (queued/blocked/failed/manual_review/...):
        # nunca mudam por submissão.
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state=order.state,
        )

    reason = _submission_gate(order, list(order.items))
    if reason is not None:
        order.last_error = reason
        order.updated_at = now
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state=order.state,
        )

    # Estágio A: transitiona para submitting e persiste antes do provider.
    order.state = "submitting"
    order.updated_at = now
    session.commit()

    # Estágio B: serializa a chamada ao provider.
    locked = (
        session.query(AliExpressOrder)
        .filter_by(id=ali_order.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if locked.state == "submitted":
        # Corrida: outra execução venceu enquanto este thread aguardava o lock.
        session.commit()
        return FulfillmentResult(
            aliexpress_order=locked,
            items=list(locked.items),
            state="submitted",
        )

    items = list(locked.items)
    try:
        result = submitter.submit(_build_submission_request(locked, items))
    except SubmissionHardOffError:
        logger.info(
            "aliexpress submission: hard-off bloqueou envio de %s", ali_order.id
        )
        return _fail(order=locked, error=ERROR_CODES["SUBMISSION_HARD_OFF"], session=session)
    except AliExpressSubmissionFailed as exc:
        logger.warning(
            "aliexpress submission: provider falhou p/ %s: %s", ali_order.id, exc
        )
        return _fail(order=locked, error=ERROR_CODES["SUBMISSION_FAILED"], session=session)
    except Exception:
        logger.exception(
            "aliexpress submission: erro interno p/ %s", ali_order.id
        )
        return _fail(order=locked, error=ERROR_CODES["SUBMISSION_ERROR"], session=session)

    if result is None or not getattr(result, "external_order_id", ""):
        return _fail(order=locked, error=ERROR_CODES["SUBMISSION_ERROR"], session=session)

    # Sucesso confirmado: persiste identidade externa e estados submitted.
    locked.state = "submitted"
    locked.submitted_at = now
    locked.ali_order_id = result.external_order_id
    locked.ali_status = result.external_status
    locked.last_error = None
    locked.attempt_count += 1
    locked.last_attempt_at = now
    locked.updated_at = now
    for item in locked.items:
        item.state = "submitted"
        item.last_error = None
        item.updated_at = now
    session.commit()
    return FulfillmentResult(
        aliexpress_order=locked,
        items=list(locked.items),
        state="submitted",
    )