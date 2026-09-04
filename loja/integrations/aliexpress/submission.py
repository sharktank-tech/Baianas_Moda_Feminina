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
from loja.modules.models import (
    AliExpressOrder,
    AliExpressOrderItem,
    AliExpressRemoteOrder,
    AliExpressSubmissionAttempt,
)
from .submission_snapshot import AliExpressSubmissionSnapshot
from loja.utils import utcnow

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

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


class RemoteOrderPersistenceAmbiguous(Exception):
    """Erro de persistência pós-provider: remote ID já pertence a outra ordem,
    falha de constraint, ou falha de DB.

    NUNCA gera AliExpressSubmissionFailed.
    Mapeia sempre para pending_reconciliation.

    Todas as instâncias carregam APENAS um código fixo sanitizado na mensagem —
    NUNCA IDs remotos, segredos ou payload do provider.
    """


# Códigos sanitizados (8.0.11B): NUNCA colocam IDs remotos em exceptions,
# repr, last_error ou logs. Fixos, não derivados de payload do provider.
REMOTE_ERROR_INVALID = "remote_order_identity_invalid"
REMOTE_ERROR_DUPLICATE = "remote_order_duplicate_identity"
REMOTE_ERROR_COLLISION = "remote_order_identity_collision"
REMOTE_ERROR_PERSISTENCE = "remote_order_persistence_ambiguous"
REMOTE_ERROR_ATTEMPT_IN_PROGRESS = "attempt_in_progress"


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
    """Resposta do provider (fake) — identidades externas confirmadas."""

    external_order_ids: tuple[str, ...]
    external_status: str = "submitted"

    @property
    def external_order_id(self) -> Optional[str]:
        if len(self.external_order_ids) == 1:
            return self.external_order_ids[0]
        raise ValueError(
            "external_order_id indefinido para cardinalidade "
            f"N={len(self.external_order_ids)}"
        )


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
        if not _valid_product_id(entry.get("product_id")):
            return ERROR_CODES["SUBMISSION_SNAPSHOT_INVALID"]
        if not str(entry.get("sku_attr") or "").strip():
            return ERROR_CODES["SUBMISSION_SNAPSHOT_INVALID"]
        if not _valid_product_count(entry.get("product_count")):
            return ERROR_CODES["SUBMISSION_SNAPSHOT_INVALID"]
        if "logistics_service_name" in entry:
            service = str(entry.get("logistics_service_name") or "").strip()
            if not service:
                return ERROR_CODES["SUBMISSION_SNAPSHOT_INVALID"]

    return None


def _valid_product_id(value) -> bool:
    """product_id deve ser inteiro decimal POSITIVO.

    Válidos: 12345, "12345". Inválidos: bool, "", "ABC", "-1", "1.2", 0, negativo.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str):
        s = value.strip()
        return bool(s) and s.isdigit() and int(s) > 0
    return False


def _valid_product_count(value) -> bool:
    """product_count deve ser INTEGER real >= 1.

    Válido: 1. Inválidos: "1" (string), 1.0/1.5 (float), True/False, 0, negativo.
    """
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 1
    )


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
            product_count=entry["product_count"],
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


def _abort_pre_provider(
    *,
    order: AliExpressOrder,
    error: str,
    session,
) -> FulfillmentResult:
    """Aborta LOCAL antes do provider (8.0.11A / 8.0.10.2 hardening).

    Usado quando o snapshot deixa de ser válido entre os estágios A e B. Como
    nenhum HTTP foi executado, NÃO vai para ``pending_reconciliation``:
      * volta a ``ready``;
      * limpa o attempt_key gerado para a tentativa que nunca saiu;
      * NÃO incrementa attempt_count;
      * preserva order/items/snapshot;
      * persiste last_error e commita.
    NUNCA substitui _fail para desfechos pós-provider (aqueles incrementam
    attempt_count e podem ir para pending_reconciliation).
    """
    order.state = "ready"
    order.attempt_key = None
    order.last_error = error
    order.updated_at = utcnow()
    session.commit()
    return FulfillmentResult(
        aliexpress_order=order,
        items=list(order.items),
        state=order.state,
    )


def _validate_remote_order_ids(external_order_ids) -> list[str]:
    """Valida e normaliza ``external_order_ids`` (8.0.11B, item 5).

    Exige tuple/list com >= 1 ID, todos strings válidas e distintas.
    Levanta ``RemoteOrderPersistenceAmbiguous`` com código FIXO sanitizado —
    NUNCA inclui os IDs ou os valores inválidos na mensagem.
    """
    if not isinstance(external_order_ids, (tuple, list)):
        raise RemoteOrderPersistenceAmbiguous(REMOTE_ERROR_INVALID)
    ids = [str(x) for x in external_order_ids]
    if not ids:
        raise RemoteOrderPersistenceAmbiguous(REMOTE_ERROR_INVALID)
    for rid in ids:
        if not isinstance(rid, str) or not rid or len(rid) > 64:
            raise RemoteOrderPersistenceAmbiguous(REMOTE_ERROR_INVALID)
    if len(set(ids)) != len(ids):
        raise RemoteOrderPersistenceAmbiguous(REMOTE_ERROR_DUPLICATE)
    return ids


def _persist_remote_orders(
    ali_order: AliExpressOrder,
    external_order_ids,
    attempt_key: Optional[str],
    session,
) -> None:
    """Persistência atômica de 0/1/N remote order IDs (8.0.11B hardening).

    Contrato:
    - valida a coleção inteira ANTES de qualquer alteração (item 5);
    - não persiste parcialmente (transação única);
    - verifica colisão global: remote ID de outra ordem -> ambiguous (item 6);
    - verifica idempotência: replay exato da MESMA ordem não duplica (item 6);
    - subset/superset/ordem diferente -> persistence ambiguous (item 6);
    - cria TODOS os AliExpressRemoteOrder;
    - atualiza AliExpressOrder, itens e attempt na MESMA transação;
    - commit único do resultado local (feito pelo chamador).

    Levanta ``RemoteOrderPersistenceAmbiguous`` com código sanitizado fixo —
    NUNCA gera AliExpressSubmissionFailed nem expõe IDs remotos.
    """
    ids = _validate_remote_order_ids(external_order_ids)

    # 1. Colisão global: remote ID já associado a OUTRA ordem.
    existing_global = (
        session.query(AliExpressRemoteOrder)
        .filter(AliExpressRemoteOrder.remote_order_id.in_(ids))
        .all()
    )
    for rem in existing_global:
        if rem.aliexpress_order_id != ali_order.id:
            raise RemoteOrderPersistenceAmbiguous(REMOTE_ERROR_COLLISION)

    # 2. Idempotência: remote orders já existentes da MESMA ordem.
    existing_same = (
        session.query(AliExpressRemoteOrder)
        .filter_by(aliexpress_order_id=ali_order.id)
        .all()
    )
    existing_ids = {r.remote_order_id for r in existing_same}
    incoming_set = set(ids)
    if existing_same:
        if existing_ids != incoming_set or len(existing_same) != len(ids):
            # subset, superset, ou composição diferente -> ambiguidade local.
            raise RemoteOrderPersistenceAmbiguous(REMOTE_ERROR_PERSISTENCE)

    # 3. Atualiza parent (ambos caminhos: fresh e replay exato).
    ali_order.state = "submitted"
    ali_order.submitted_at = utcnow()
    ali_order.attempt_count += 1
    ali_order.last_attempt_at = utcnow()
    ali_order.updated_at = utcnow()
    ali_order.last_error = None
    if len(ids) == 1:
        ali_order.ali_order_id = ids[0]
    else:
        ali_order.ali_order_id = None
    ali_order.ali_status = "submitted"

    # 4. Cria filhos SOMENTE se replay exato NÃO existir.
    if not existing_same:
        for position, remote_id in enumerate(ids):
            remote_order = AliExpressRemoteOrder(
                aliexpress_order_id=ali_order.id,
                remote_order_id=remote_id,
                position=position,
                attempt_key=attempt_key,
                remote_status="submitted",
            )
            session.add(remote_order)

    # 5. Marcar itens
    for item in ali_order.items:
        item.state = "submitted"
        item.last_error = None
        item.updated_at = utcnow()

    # 6. Atualizar attempt (confirmado no sucesso)
    attempt = (
        session.query(AliExpressSubmissionAttempt)
        .filter_by(
            aliexpress_order_id=ali_order.id,
            attempt_key=attempt_key,
        )
        .first()
    )
    if attempt:
        attempt.state = "confirmed"
        attempt.completed_at = utcnow()


def _persist_error_code(exc: Exception) -> str:
    """Mapeia uma exceção de persistência para código sanitizado fixo.

    NUNCA usa o texto (``str(exc)``) de exceções de DB/provider, que poderia
    conter IDs remotos ou payload. Retorna sempre um código fixo.
    """
    if isinstance(exc, RemoteOrderPersistenceAmbiguous):
        msg = str(exc)
        if msg == REMOTE_ERROR_DUPLICATE:
            return REMOTE_ERROR_DUPLICATE
        if msg == REMOTE_ERROR_COLLISION:
            return REMOTE_ERROR_COLLISION
        if msg == REMOTE_ERROR_INVALID:
            return REMOTE_ERROR_INVALID
        return REMOTE_ERROR_PERSISTENCE
    # IntegrityError / SQLAlchemyError / qualquer outra falha de DB.
    return REMOTE_ERROR_PERSISTENCE


def _recover_from_persist_failure(
    session,
    ali_order_id,
    attempt_key,
    error_code: str,
) -> FulfillmentResult:
    """Recupera uma falha de persistência PÓS-provider (8.0.11B, item 3).

    A transação atual (que mantém intent do provider) é ROLLBACK.
    Em NOVA transação, re-fetch de order e attempt, e persiste best-effort:
      * attempt -> ``unknown`` (completed);
      * parent  -> ``pending_reconciliation`` (NUNCA AliExpressSubmissionFailed);
      * last_error = código sanitizado (sem IDs remotos).

    Se o commit de recuperação TAMBÉM falhar:
      * NÃO executa o provider novamente (nenhum HTTP duplicado);
      * o attempt permanece ``in_flight`` -> continua sendo barreira
        (stale barrier) para submissões posteriores.
    """
    session.rollback()
    error_code = error_code or REMOTE_ERROR_PERSISTENCE

    try:
        order = (
            session.query(AliExpressOrder)
            .filter_by(id=ali_order_id)
            .first()
        )
    except Exception:
        session.rollback()
        order = None

    try:
        attempt = (
            session.query(AliExpressSubmissionAttempt)
            .filter_by(
                aliexpress_order_id=ali_order_id,
                attempt_key=attempt_key,
            )
            .first()
        )
        if attempt is not None:
            attempt.state = "unknown"
            attempt.completed_at = utcnow()
            attempt.last_error = error_code
        if order is not None:
            order.state = "pending_reconciliation"
            order.last_error = error_code
            order.updated_at = utcnow()
        session.commit()
        state = "pending_reconciliation"
    except Exception:
        # Commit de recuperação falhou: não executar provider; o attempt
        # permanece in_flight e continua como barreira.
        session.rollback()
        state = "pending_reconciliation"

    if order is None:
        # Não foi possível re-fetch: constrói resultado sem tocar em itens.
        return FulfillmentResult(
            aliexpress_order=ali_order_id,
            items=[],
            state=state,
        )

    try:
        items = list(order.items)
    except Exception:
        session.rollback()
        items = []

    return FulfillmentResult(
        aliexpress_order=order,
        items=items,
        state=state,
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
    # reconciliação da mesma tentativa. NENHUM SubmissionAttempt é criado aqui.
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

    # Stale attempt barrier (8.0.11B, item 2): ANTES de criar o novo attempt,
    # procura QUALQUER attempt RESOLVIDO desta AliExpressOrder em estado
    # ``in_flight`` ou ``unknown`` com ``completed_at IS NULL`` — de QUALQUER
    # attempt_key (NÃO exclui o mesmo attempt_key). Um attempt ``unknown`` com
    # ``completed_at`` preenchido é terminal: a reconciliação autoritária já o
    # resolveu (ex.: RECONCILE_NOT_FOUND -> ready para retry), portanto NÃO
    # bloqueia. Se existir attempt não resolvido:
    #   * zero novo attempt criado;
    #   * zero chamada ao provider;
    #   * parent -> ``pending_reconciliation`` com código sanitizado;
    #   * NUNCA IntegrityError por attempt_key duplicado (não insere).
    stale_attempt = (
        session.query(AliExpressSubmissionAttempt)
        .filter(
            AliExpressSubmissionAttempt.aliexpress_order_id
            == ali_order.id
        )
        .filter(
            AliExpressSubmissionAttempt.state.in_(["in_flight", "unknown"])
        )
        .filter(
            AliExpressSubmissionAttempt.completed_at.is_(None)
        )
        .first()
    )
    if stale_attempt is not None:
        locked.state = "pending_reconciliation"
        locked.last_error = REMOTE_ERROR_ATTEMPT_IN_PROGRESS
        locked.updated_at = utcnow()
        session.commit()
        return FulfillmentResult(
            aliexpress_order=locked,
            items=list(locked.items),
            state="pending_reconciliation",
        )

    # Stage B preflight REAL: revalida o snapshot sob o lock do Estágio B,
    # IMEDIATAMENTE antes de criar attempt/provider. Como NENHUM HTTP aconteceu,
    # NÃO marcamos pending_reconciliation: voltamos para ``ready``, limpamos o
    # attempt_key da tentativa que NUNCA saiu e NÃO incrementamos attempt_count.
    # Zero SubmissionAttempt é criado nesta falha (barreira movida para depois).
    stage_b_error = _preflight_submission_snapshot(session, ali_order.id)
    if stage_b_error is not None:
        return _abort_pre_provider(
            order=locked,
            error=stage_b_error,
            session=session,
        )

    # Tudo aprovado: criar a barreira durável ``in_flight`` e COMMIT antes do
    # provider (8.0.11B, item 1). A partir daqui o attempt é a prova de que a
    # submissão está em andamento; se o processo morrer, ele fica ``in_flight``
    # e bloqueia (stale barrier) qualquer retry até reconciliação remota.
    attempt = AliExpressSubmissionAttempt(
        aliexpress_order_id=locked.id,
        attempt_key=locked.attempt_key,
        state="in_flight",
        started_at=now,
        completed_at=None,
        last_error=None,
        created_at=now,
        updated_at=now,
    )
    session.add(attempt)
    session.commit()

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
        # Marcar attempt como rejected
        attempt.state = "rejected"
        attempt.completed_at = utcnow()
        attempt.last_error = ERROR_CODES["SUBMISSION_HARD_OFF"]
        session.commit()
        return _fail(
            order=locked, error=ERROR_CODES["SUBMISSION_HARD_OFF"], session=session
        )
    except AliExpressSubmissionFailed:
        # Falha CONFIRMADA pelo provider: mantém submitting (retry síncrono).
        logger.warning(
            "aliexpress submission: provider falhou p/ %s code=%s",
            ali_order.id, ERROR_CODES["SUBMISSION_FAILED"],
        )
        # Marcar attempt como rejected
        attempt.state = "rejected"
        attempt.completed_at = utcnow()
        attempt.last_error = ERROR_CODES["SUBMISSION_FAILED"]
        session.commit()
        return _fail(
            order=locked, error=ERROR_CODES["SUBMISSION_FAILED"], session=session
        )
    except AliExpressSubmissionUnknown:
        # Resultado AMBÍGUO (timeout/desconexão): pode ter criado remotamente.
        logger.warning(
            "aliexpress submission: resultado desconhecido p/ %s code=%s",
            ali_order.id, ERROR_CODES["SUBMISSION_UNKNOWN"],
        )
        # Marcar attempt como unknown, order para pending_reconciliation
        attempt.state = "unknown"
        attempt.completed_at = utcnow()
        locked.attempt_count += 1
        locked.last_attempt_at = now
        locked.state = "pending_reconciliation"
        locked.last_error = ERROR_CODES["SUBMISSION_UNKNOWN"]
        locked.updated_at = now
        session.commit()
        return FulfillmentResult(
            aliexpress_order=locked,
            items=list(locked.items),
            state="pending_reconciliation",
        )
    except Exception:
        # Exceção inesperada durante a chamada: conservadoramente ambíguo.
        # Nunca logar a exceção (o texto do provider pode conter segredos).
        logger.error(
            "aliexpress submission: erro interno p/ %s code=%s",
            ali_order.id, ERROR_CODES["SUBMISSION_UNKNOWN"],
        )
        attempt.state = "unknown"
        attempt.completed_at = utcnow()
        locked.attempt_count += 1
        locked.last_attempt_at = now
        locked.state = "pending_reconciliation"
        locked.last_error = ERROR_CODES["SUBMISSION_UNKNOWN"]
        locked.updated_at = now
        session.commit()
        return FulfillmentResult(
            aliexpress_order=locked,
            items=list(locked.items),
            state="pending_reconciliation",
        )

    # Validar resultado do provider (8.0.11B: tuple-based). Identidade ausente
    # ou vazia -> ambíguo, NUNCA "não criado" (pode ter criado remotamente).
    external_order_ids = getattr(result, "external_order_ids", None) or ()
    if not external_order_ids:
        logger.error(
            "aliexpress submission: resposta sem identidade p/ %s code=%s",
            ali_order.id, ERROR_CODES["SUBMISSION_UNKNOWN"],
        )
        attempt.state = "unknown"
        attempt.completed_at = utcnow()
        locked.attempt_count += 1
        locked.last_attempt_at = now
        locked.state = "pending_reconciliation"
        locked.last_error = ERROR_CODES["SUBMISSION_UNKNOWN"]
        locked.updated_at = now
        session.commit()
        return FulfillmentResult(
            aliexpress_order=locked,
            items=list(locked.items),
            state="pending_reconciliation",
        )

    # Sucesso: persistência atômica de 0/1/N remote order IDs + commit DENTRO
    # do mesmo try (8.0.11B, item 3). Captura IntegrityError/SQLAlchemyError/
    # RemoteOrderPersistenceAmbiguous -> rollback + nova transação de recuperação.
    try:
        _persist_remote_orders(
            ali_order=locked,
            external_order_ids=external_order_ids,
            attempt_key=locked.attempt_key,
            session=session,
        )
        session.commit()
    except (IntegrityError, SQLAlchemyError, RemoteOrderPersistenceAmbiguous) as e:
        code = _persist_error_code(e)
        return _recover_from_persist_failure(
            session=session,
            ali_order_id=locked.id,
            attempt_key=locked.attempt_key,
            error_code=code,
        )

    return FulfillmentResult(
        aliexpress_order=locked,
        items=list(locked.items),
        state="submitted",
    )