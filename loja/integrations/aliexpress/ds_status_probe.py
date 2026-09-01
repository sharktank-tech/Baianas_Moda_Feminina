"""Probe MANUAL read-only de status DS AliExpress (Fase 8.0.7A.1) — opt-in.

Nunca roda automaticamente. Para executar (acionamento explícito):

    ALIEXPRESS_ORDER_STATUS_PROBE=1 \
    ALIEXPRESS_APP_KEY=... ALIEXPRESS_APP_SECRET=... \
    ALIEXPRESS_CREDENTIAL_KEY=... DATABASE_URL=... \
    python -m loja.integrations.aliexpress.ds_status_probe \
        <supplier_id> <ali_order_id>

Ordem de execução (falha fechada):
    1. valida ``ALIEXPRESS_ORDER_STATUS_PROBE == "1"``;
    2. valida argumentos básicos (supplier UUID + ali_order_id informado);
    3. cria a aplicação via factory oficial ``loja.create_app``;
    4. entra em ``app.app_context()``;
    5. vincula a ordem local: UMA AliExpressOrder com
       ``supplier_id == supplier informado`` E ``ali_order_id == argumento``;
    6. verifica estado elegível (submitted/confirmed/shipped/delivered);
    7. executa exatamente UMA consulta read-only via
       ``AliExpressDSOrderStatusSyncer.sync(request)``.
    Sem a flag: zero app, zero DB, zero HTTP, exit 2.

BIND LOCAL OBRIGATÓRIO:
    O probe NÃO consulta um ali_order_id arbitrário. Sem uma ordem local
    correspondente (exatamente uma, com ambos os vínculos), falha fechado
    antes do adapter: zero HTTP, exit 1, mensagem sanitizada.

ESTADO ELEGÍVEL:
    Mesmo conceito do motor 8.0.6 (identidade externa confiável pós-submissão):
    submitted/confirmed/shipped/delivered. queued/blocked/ready/submitting/
    pending_reconciliation/failed/manual_review/pending/cancel_requested/
    cancelled NUNCA consultam o provider (cancelled é terminal local).

REQUEST:
    Usa os IDs REAIS da ordem encontrada (aliexpress_order_id = id local,
    order_id/supplier_id/ali_order_id/attempt_key = valores persistidos) e
    chama o adapter diretamente — jamais ``sync_aliexpress_order_status``.

READ-ONLY:
    NÃO modifica AliExpressOrder (state/milestones/ali_status/last_error),
    NÃO cria Shipment/TrackingEvent, NÃO submete/reconcilia/cancela e NÃO
    consulta catálogo. `get_fulfillment_access_token` pode marcar credencial
    expirada via flush (comportamento existente do credential_store, aqui
    apenas documentado — nenhuma lógica nova de credencial nesta correção).

TRANSAÇÃO:
    Em qualquer exceção, ``db.session.rollback()`` antes de sair. Em sucesso,
    nada é commitado (consulta é read-only); o processo encerra e a sessão
    é removida pelo final do app context.

SAÍDA (sanitizada):
    success/failure, method, outcome, request_id (se seguro). Nunca imprimi
    ali_order_id completo, supplier_id, token, secret, session, URL assinada,
    query params, raw body/payload ou headers (order_status/logistics_status
    NÃO são impressos — o adapter não os expõe e o boundary não é ampliado).
"""

from __future__ import annotations

import os
import sys
import uuid
from typing import Optional, Tuple

_PROBE_ENV = "ALIEXPRESS_ORDER_STATUS_PROBE"

_EXIT_CLOSED = 2
_EXIT_FAILURE = 1
_EXIT_SUCCESS = 0

#: Mesmos valores canônicos do motor 8.0.6 (status_sync.STATUS_*).
_ELIGIBLE_STATES = frozenset(
    {"submitted", "confirmed", "shipped", "delivered"}
)


def _probe_enabled() -> bool:
    return os.environ.get(_PROBE_ENV) == "1"


def _close_fail_closed() -> str:
    return (
        f"PROBE fechado: {_PROBE_ENV} != 1 (manual, read-only, opt-in). "
        "Nenhuma chamada HTTP ou de banco foi executada."
    )


def _validate_args(supplier_id: str, ali_order_id: str) -> Optional[Tuple[uuid.UUID, str]]:
    """Valida argumentos básicos SEM abrir app/DB/HTTP."""
    try:
        supplier_uuid = uuid.UUID(str(supplier_id).strip())
    except (ValueError, TypeError, AttributeError):
        return None
    value = (ali_order_id or "").strip()
    if not value:
        return None
    return supplier_uuid, value


def _binding_error_message() -> str:
    # Genérica: não expõe supplier_id/ali_order_id sensíveis.
    return "ordem local não encontrada para consulta DS"


def run_probe(supplier_id: str, ali_order_id: str) -> int:
    """Executa UMA consulta read-only após binding local obrigatório (adapter puro)."""
    if not _probe_enabled():
        print(_close_fail_closed())
        return _EXIT_CLOSED

    parsed = _validate_args(supplier_id, ali_order_id)
    if parsed is None:
        print("[ds-status-probe] failure argumentos inválidos (supplier UUID e ali_order_id exigidos)")
        return _EXIT_FAILURE
    supplier_uuid, value = parsed

    from loja import create_app  # factory oficial (guard de opt-in já passou)

    app = create_app()
    with app.app_context():
        from loja import db
        from loja.modules.models import AliExpressOrder

        rows = (
            AliExpressOrder.query.filter_by(
                supplier_id=supplier_uuid, ali_order_id=value
            ).all()
        )
        if len(rows) != 1:
            print("[ds-status-probe] failure", _binding_error_message())
            return _EXIT_FAILURE
        ali = rows[0]

        if ali.state not in _ELIGIBLE_STATES:
            print("[ds-status-probe] failure ordem em estado não elegível para consulta DS")
            return _EXIT_FAILURE

        from loja.integrations.aliexpress.ds_order_status import (
            DS_ORDER_STATUS_METHOD,
            AliExpressDSOrderStatusSyncer,
        )
        from loja.integrations.aliexpress.status_sync import (
            AliExpressOrderStatusRequest,
            AliExpressOrderStatusSyncError,
        )

        # Request com os IDs REAIS persistidos (contrato real do sistema).
        request = AliExpressOrderStatusRequest(
            aliexpress_order_id=ali.id,
            order_id=ali.order_id,
            supplier_id=ali.supplier_id,
            ali_order_id=ali.ali_order_id,
            attempt_key=ali.attempt_key,
        )
        syncer = AliExpressDSOrderStatusSyncer()
        try:
            result = syncer.sync(request)
        except AliExpressOrderStatusSyncError as exc:
            db.session.rollback()
            print("[ds-status-probe] failure", str(exc))
            return _EXIT_FAILURE
        except Exception:
            db.session.rollback()
            print("[ds-status-probe] failure erro interno (sem detalhes)")
            return _EXIT_FAILURE

        # Sucesso: nada é persistido (read-only). Credential_store pode ter
        # marcado credencial expirada via flush — comportamento existente,
        # mantido sem lógica nova; a sessão morre com o app context.
        print("[ds-status-probe] success")
        print("[ds-status-probe] method", DS_ORDER_STATUS_METHOD)
        print("[ds-status-probe] outcome", result.outcome)
        if syncer.last_request_id:
            print("[ds-status-probe] request_id", syncer.last_request_id)
        return _EXIT_SUCCESS


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not _probe_enabled():
        print(_close_fail_closed())
        return _EXIT_CLOSED
    if len(argv) != 2:
        print(
            "uso: python -m loja.integrations.aliexpress.ds_status_probe "
            "<supplier_id> <ali_order_id>"
        )
        return _EXIT_FAILURE
    return run_probe(argv[0], argv[1])


if __name__ == "__main__":
    sys.exit(main())