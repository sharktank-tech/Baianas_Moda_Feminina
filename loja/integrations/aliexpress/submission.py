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
    * falha CONFIRMADA preserva a ordem/itens/id/snapshot e mantém
      ``submitting`` com ``last_error`` sanitizado e ``attempt_count + 1``
      para retry síncrono.

DESFECHOS DO PROVIDER (Fase 8.0.5):
    * sucesso confirmado    -> ``AliExpressSubmissionResult`` (external id)
    * falha confirmada      -> ``AliExpressSubmissionFailed``
                              (mantém ``submitting``, retry síncrono seguro)
    * desconhecido/ambíguo  -> ``AliExpressSubmissionUnknown``
                              (timeout/desconexão; RESULTADO REMOTO PODE TER
                              ACONTECIDO). A ordem vai para
                              ``pending_reconciliation``: não volta a ``ready``,
                              não permite nova criação e exige reconciliação
                              explícita (8.0.5) antes de novo submit.
    * qualquer outra exceção do provider é conservadoramente tratada como
      desconhecido (nunca interpretar timeout genérico como "não criado").

IDEMPOTÊNCIA (por tentativa):
    ``AliExpressOrder.attempt_key`` é a chave local estável da tentativa
    corrente: gerada deterministicamente de (order.id, seq) ANTES da chamada
    lógica ao provider, estável durante a reconciliação da mesma tentativa,
    sem segredo e sem dependência do catálogo. Retry após conclusão
    ``not_found`` gera nova chave (novo seq).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Optional

from loja import db
from loja.modules.models import AliExpressOrder, AliExpressOrderItem
from .submission_snapshot import AliExpressSubmissionSnapshot
from loja.utils import utcnow

from .fulfillment import ERROR_CODES, FulfillmentResult, _check_credential
from .oauth_refresh import evaluate_credential_with_refresh

logger = logging.getLogger(__name__)


class SubmissionHardOffError(Exception):
    """Criação real de pedido AliExpress está desabilitada (HARD-OFF 8.0.3)."""


class AliExpressSubmissionFailed(Exception):
    """Falha confirmada do provider (fake) na submissão — retry seguro."""


class AliExpressSubmissionUnknown(Exception):
    """Resultado desconhecido/ambíguo (timeout, desconexão) — a ordem pode ter
    sido criada remotamente; requer reconciliação antes de nova tentativa."""


def _attempt_key(order: AliExpressOrder) -> str:
    """Chave determinística de TENTATIVA: sha256('alias:attempt:<order>:<seq>').

    ``seq = order.attempt_count + 1``: criada antes da chamada ao provider,
    estável durante a reconciliação da mesma tentativa, sem segredo e sem
    dependência do catálogo.
    """
    seq = (order.attempt_count or 0) + 1
    base = f"alias:submission-attempt:v1:{order.id}:{seq}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AliExpressSubmissionItem:
    """Item de submissão LEGACY (Fase 8.0.3), baseado em AliExpressOrderItem.

    Mantido para compatibilidade de imports externos; o caminho usado pelo
    provider (Fase 8.0.10.2) é 100% canônico via ``AliExpressPlaceOrderItem``.
    """

    order_item_id: uuid.UUID
    supplier_product_external_id: str
    supplier_sku_external_id: str
    quantity: int
    supplier_unit_cost: Optional[Decimal]
    supplier_currency: Optional[str]
    variant_label: Optional[str]


@dataclass(frozen=True)
class AliExpressPlaceOrderItem:
    """Item canônico do futuro placeorder — fonte exclusiva: submission snapshot.

    Mapeia 1:1 os campos persistidos em ``AliExpressSubmissionSnapshot.items``:
       * ``product_id``  -> supplier product external id (congelado);
       * ``sku_attr``    -> supplier_sku_attr congelado ``pid:vid;pid:vid``;
       * ``product_count`` -> quantidade congelada;
       * ``logistics_service_name`` -> OPCIONAL (omitido quando desconhecido).
    """

    product_id: str
    sku_attr: str
    product_count: int
    logistics_service_name: Optional[str] = None


@dataclass(frozen=True)
class AliExpressSubmissionRequest:
    """Payload canônico do futuro placeorder — fonte exclusiva: submission snapshot.

    ``shipping_address`` e ``items`` vêm inteiramente do snapshot persistido;
    nenhum dado é reconstruído do catálogo (Order.address, ProductVariant,
    AliExpressOrderItem) no instante da escrita.
    """

    aliexpress_order_id: uuid.UUID
    order_id: Optional[uuid.UUID]
    supplier_id: Optional[uuid.UUID]
    attempt_key: Optional[str]
    shipping_address: Mapping[str, str]
    items: tuple[AliExpressPlaceOrderItem, ...]


@dataclass(frozen=True)
class AliExpressSubmissionResult:
    """Resposta do provider (fake) — identidade externa confirmada."""

    external_order_id: str
    external_status: str = "submitted"


class AliExpressOrderSubmitter(ABC):
    """Boundary de submissão: o domínio nunca fala HTTP diretamente."""

    @abstractmethod
    def submit(self, request: AliExpressSubmissionRequest) -> AliExpressSubmissionResult:
        """Envia o pedido (Fase 8.0.5 — desfechos):
          * sucesso confirmado: retorna ``AliExpressSubmissionResult``;
          * falha confirmada: levanta ``AliExpressSubmissionFailed``;
          * desconhecido/ambíguo (timeout/desconexão): levanta
            ``AliExpressSubmissionUnknown``.
        """


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


def _validate_submission_snapshot(snapshot) -> Optional[str]:
    """PRE-FLIGHT canônico: valida minimamente o submission snapshot.

    Retorna None (válido) ou um código sanitizado. Nada é reconstruído do
    catálogo; apenas a integridade estrutural do snapshot congelado é checada.

    Contrato:
      * shipping_address: Mapping não vazio;
      * items: lista não vazia;
      * cada item: product_id non-empty, sku_attr non-empty,
        product_count inteiro >= 1;
      * logistics_service_name: se a chave AUSENTE -> válido; se presente ->
        string non-empty após strip.
    """
    if snapshot is None:
        return ERROR_CODES["SUBMISSION_SNAPSHOT_MISSING"]

    shipping = getattr(snapshot, "shipping_address", None)
    if not isinstance(shipping, Mapping) or not shipping:
        return ERROR_CODES["SUBMISSION_SNAPSHOT_INVALID"]

    items = getattr(snapshot, "items", None)
    if not isinstance(items, (list, tuple)) or not items:
        return ERROR_CODES["SUBMISSION_SNAPSHOT_INVALID"]

    for entry in items:
        if not isinstance(entry, Mapping):
            return ERROR_CODES["SUBMISSION_SNAPSHOT_INVALID"]
        if not str(entry.get("product_id") or "").strip():
            return ERROR_CODES["SUBMISSION_SNAPSHOT_INVALID"]
        if not str(entry.get("sku_attr") or "").strip():
            return ERROR_CODES["SUBMISSION_SNAPSHOT_INVALID"]
        try:
            count = int(entry.get("product_count") or 0)
        except (TypeError, ValueError):
            return ERROR_CODES["SUBMISSION_SNAPSHOT_INVALID"]
        if count < 1:
            return ERROR_CODES["SUBMISSION_SNAPSHOT_INVALID"]
        if "logistics_service_name" in entry:
            service = str(entry.get("logistics_service_name") or "").strip()
            if not service:
                return ERROR_CODES["SUBMISSION_SNAPSHOT_INVALID"]

    return None


def _preflight_submission_snapshot(session, aliexpress_order_id) -> Optional[str]:
    """PRE-FLIGHT eficiente: busca o snapshot (mesma session) e valida.

    Retorna None se um snapshot válido existe; caso contrário um código
    sanitizado (``submission_snapshot_missing`` ou ``submission_snapshot_invalid``).
    """
    snapshot = (
        session.query(AliExpressSubmissionSnapshot)
        .filter_by(aliexpress_order_id=aliexpress_order_id)
        .first()
    )
    return _validate_submission_snapshot(snapshot)


def _build_submission_request(
    order: AliExpressOrder,
    submission_snapshot,
) -> AliExpressSubmissionRequest:
    """Monta o payload canônico SOMENTE a partir do submission snapshot.

    ``shipping_address`` e ``items`` vêm exclusivamente do snapshot; não acessa
    catálogo (Product/ProductVariant/SupplierProduct/Order.address).
    """
    items = tuple(
        AliExpressPlaceOrderItem(
            product_id=str(entry["product_id"]),
            sku_attr=str(entry["sku_attr"]),
            product_count=int(entry["product_count"]),
            logistics_service_name=(
                str(entry["logistics_service_name"]).strip()
                if entry.get("logistics_service_name")
                else None
            ),
        )
        for entry in submission_snapshot.items
    )
    return AliExpressSubmissionRequest(
        aliexpress_order_id=order.id,
        order_id=order.order_id,
        supplier_id=order.supplier_id,
        attempt_key=order.attempt_key,
        shipping_address=dict(submission_snapshot.shipping_address),
        items=items,
    )


def _submission_gate(
    order: AliExpressOrder,
    items: list[AliExpressOrderItem],
    refresher=None,
) -> Optional[str]:
    """Barreiras que impedem a submissão (nunca chegam ao provider).

    Credencial NUNCA supera erro de integridade de item: itens inválidos
    bloqueiam antes da checagem de auth. Retorna None se a ordem pode ser
    enviada ao provider.

    ``refresher`` (Fase 8.0.4): quando informado, credencial expirada com
    refresh token disponível tenta renovação síncrona; sem ele, comportamento
    idêntico à Fase 8.0.3 (sem renovação automática).
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

    if refresher is None:
        return _check_credential(order.supplier_id)
    return evaluate_credential_with_refresh(order.supplier_id, refresher)


def _fail(
    *,
    order: AliExpressOrder,
    error: str,
    session,
    state: Optional[str] = "submitting",
) -> FulfillmentResult:
    """Persiste um desfecho de submissão preservando ordem e snapshot.

    ``state``:
      * ``submitting`` — falha confirmada (retry síncrono seguro);
      * ``pending_reconciliation`` — resultado ambíguo/desconhecido (8.0.5):
        não volta a ``ready`` e exige reconciliação antes de novo envio.
    """
    order.state = state
    order.last_error = error
    attempt_key = order.attempt_key or _attempt_key(order)
    order.attempt_count += 1
    order.attempt_key = attempt_key
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
    refresher=None,
    session=None,
) -> FulfillmentResult:
    """Submete uma AliExpressOrder via máquina de estados idempotente.

    Locking (PostgreSQL): usa ``SELECT ... FOR UPDATE`` em dois estágios para
    serializar a chamada ao provider e impedir dupla submissão externa:
       Estágio A: trava, checa estado/gate, gera ``attempt_key`` (8.0.5),
                  transiciona ``ready -> submitting`` e commit (submitting e
                  attempt_key persistidos ANTES do provider).
       Estágio B: re-trava, re-checa ``submitted`` (idempotência sob corrida),
                  chama o provider com o lock segurado e commit do resultado.

    Desfechos (8.0.5):
       * sucesso confirmado           -> ``submitted``;
       * falha confirmada             -> ``submitting`` (retry síncrono);
       * desconhecido/ambíguo         -> ``pending_reconciliation``: NÃO volta
         a ``ready``, NÃO permite nova criação e exige reconciliação explícita
         antes de novo envio.
    Estados ``pending_reconciliation`` NÃO são elegíveis para submissão.

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

    reason = _submission_gate(order, list(order.items), refresher=refresher)
    if reason is not None:
        order.last_error = reason
        order.updated_at = now
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state=order.state,
        )

    # PRE-FLIGHT canônico (8.0.10.2): antes de gerar attempt_key/incrementar
    # attempt_count/transicionar para submitting/chamar provider, deve existir
    # exatamente um AliExpressSubmissionSnapshot VÁLIDO. Se ausente ou inválido:
    # mantém ``ready``, NÃO gera attempt_key, NÃO chama provider, persiste
    # last_error e retorna — nunca envolto em AliExpressSubmissionFailed.
    preflight_error = _preflight_submission_snapshot(session, order.id)
    if preflight_error is not None:
        order.last_error = preflight_error
        order.updated_at = now
        session.commit()
        return FulfillmentResult(
            aliexpress_order=order,
            items=list(order.items),
            state=order.state,
        )

    # Estágio A: transiciona para submitting e persiste antes do provider,
    # incluindo a chave de tentativa/idempotência (8.0.5) — estável durante a
    # reconciliação da mesma tentativa.
    order.state = "submitting"
    order.attempt_key = _attempt_key(order)
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
    if locked.state == "pending_reconciliation":
        # Corrida com reconciliação/outro desfecho ambíguo: nunca chamar provider.
        session.commit()
        return FulfillmentResult(
            aliexpress_order=locked,
            items=list(locked.items),
            state=locked.state,
        )

    items = list(locked.items)
    try:
        snapshot = (
            session.query(AliExpressSubmissionSnapshot)
            .filter_by(aliexpress_order_id=ali_order.id)
            .first()
        )
        result = submitter.submit(_build_submission_request(locked, snapshot))
    except SubmissionHardOffError:
        logger.info(
            "aliexpress submission: hard-off bloqueou envio de %s", ali_order.id
        )
        return _fail(
            order=locked, error=ERROR_CODES["SUBMISSION_HARD_OFF"], session=session
        )
    except AliExpressSubmissionFailed:
        # Falha CONFIRMADA pelo provider: mantém submitting (retry síncrono).
        logger.warning(
            "aliexpress submission: provider falhou p/ %s code=%s",
            ali_order.id, ERROR_CODES["SUBMISSION_FAILED"],
        )
        return _fail(
            order=locked, error=ERROR_CODES["SUBMISSION_FAILED"], session=session
        )
    except AliExpressSubmissionUnknown:
        # Resultado AMBÍGUO (timeout/desconexão): pode ter criado remotamente.
        logger.warning(
            "aliexpress submission: resultado desconhecido p/ %s code=%s",
            ali_order.id, ERROR_CODES["SUBMISSION_UNKNOWN"],
        )
        return _fail(
            order=locked,
            error=ERROR_CODES["SUBMISSION_UNKNOWN"],
            state="pending_reconciliation",
            session=session,
        )
    except Exception:
        # Exceção inesperada durante a chamada: conservadoramente ambíguo.
        # Nunca logar a exceção (o texto do provider pode conter segredos).
        logger.error(
            "aliexpress submission: erro interno p/ %s code=%s",
            ali_order.id, ERROR_CODES["SUBMISSION_UNKNOWN"],
        )
        return _fail(
            order=locked,
            error=ERROR_CODES["SUBMISSION_UNKNOWN"],
            state="pending_reconciliation",
            session=session,
        )

    if result is None or not getattr(result, "external_order_id", ""):
        # Provider não confirmou identidade externa: ambíguo, não "não criado".
        logger.error(
            "aliexpress submission: resposta sem identidade p/ %s code=%s",
            ali_order.id, ERROR_CODES["SUBMISSION_UNKNOWN"],
        )
        return _fail(
            order=locked,
            error=ERROR_CODES["SUBMISSION_UNKNOWN"],
            state="pending_reconciliation",
            session=session,
        )

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