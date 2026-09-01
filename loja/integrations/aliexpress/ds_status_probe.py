"""Probe MANUAL read-only de status DS AliExpress (Fase 8.0.7A) — opt-in.

Nunca roda automaticamente. Para executar (acionamento explícito):

    ALIEXPRESS_ORDER_STATUS_PROBE=1 \
    ALIEXPRESS_APP_KEY=... ALIEXPRESS_APP_SECRET=... \
    ALIEXPRESS_CREDENTIAL_KEY=... DATABASE_URL=... \
    python -m loja.integrations.aliexpress.ds_status_probe \
        <supplier_id> <ali_order_id>

Pré-requisitos:
    * uma credencial AliExpress ativa (access_token fernet) persistida para o
      supplier_id informado — resolvida via credential_store (request.supplier_id);
    * <ali_order_id> = ID remoto (AliExpress order id) já persistido em uma
      AliExpressOrder real — NUNCA usar valores fake/forjados contra produção;
    * ALIEXPRESS_APP_KEY / ALIEXPRESS_APP_SECRET da aplicação.

Restrições (falha fechada):
    * sem ``ALIEXPRESS_ORDER_STATUS_PROBE=1`` (ou != 1): imprime o fechamento e
      sai com código 2 — ZERO chamadas HTTP;
    * leitura apenas: NÃO modifica AliExpressOrder, NÃO submete, NÃO reconcilia,
      NÃO cancela e NÃO cria Shipment/TrackingEvent;
    * nunca imprime token, secret, sessão, query/assinatura, cabeçalhos, body ou
      a resposta completa — apenas outcome + status sanitizados + request_id.

Saída sanitizada:
    success | failure, method, outcome (canônico), order_status, logistics_status,
    request_id (se seguro). Nenhum secret.
"""

from __future__ import annotations

import os
import sys
import uuid
from typing import Optional

_PROBE_ENV = "ALIEXPRESS_ORDER_STATUS_PROBE"

_EXIT_CLOSED = 2
_EXIT_FAILURE = 1
_EXIT_SUCCESS = 0


def _probe_enabled() -> bool:
    return os.environ.get(_PROBE_ENV) == "1"


def _close_fail_closed() -> str:
    return (
        f"PROBE fechado: {_PROBE_ENV} != 1 (manual, read-only, opt-in). "
        "Nenhuma chamada HTTP foi executada."
    )


def run_probe(supplier_id: str, ali_order_id: str) -> int:
    """Executa uma única consulta read-only sanitizada (adapter DS puro)."""
    from loja.integrations.aliexpress.ds_order_status import (
        DS_ORDER_STATUS_METHOD,
        AliExpressDSOrderStatusSyncer,
    )
    from loja.integrations.aliexpress.status_sync import (
        AliExpressOrderStatusRequest,
        AliExpressOrderStatusSyncError,
    )

    if not _probe_enabled():
        print(_close_fail_closed())
        return _EXIT_CLOSED

    try:
        supplier_uuid = uuid.UUID(str(supplier_id).strip())
    except (ValueError, TypeError):
        print("[ds-status-probe] failure supplier_id inválido (UUID exigido)")
        return _EXIT_FAILURE

    if not ali_order_id or not str(ali_order_id).strip():
        print("[ds-status-probe] failure ali_order_id ausente")
        return _EXIT_FAILURE

    if not os.environ.get("ALIEXPRESS_APP_KEY") or not os.environ.get(
        "ALIEXPRESS_APP_SECRET"
    ):
        print("[ds-status-probe] failure ALIEXPRESS_APP_KEY/ALIEXPRESS_APP_SECRET ausentes")
        return _EXIT_FAILURE

    # aliexpress_order_id local não existe que neste probe (nunca persiste nada);
    # o adapter só usa supplier_id/ali_order_id.
    request = AliExpressOrderStatusRequest(
        aliexpress_order_id=uuid.uuid4(),
        order_id=None,
        supplier_id=supplier_uuid,
        ali_order_id=str(ali_order_id).strip(),
        attempt_key=None,
    )
    syncer = AliExpressDSOrderStatusSyncer()
    try:
        result = syncer.sync(request)
    except AliExpressOrderStatusSyncError as exc:
        print("[ds-status-probe] failure", str(exc))
        return _EXIT_FAILURE
    except Exception:
        print("[ds-status-probe] failure erro interno (sem detalhes)")
        return _EXIT_FAILURE

    # request_id é opcional e já foi sanitizado pelo adapter.
    request_id: Optional[str] = syncer.last_request_id
    print("[ds-status-probe] success")
    print("[ds-status-probe] method", DS_ORDER_STATUS_METHOD)
    print("[ds-status-probe] outcome", result.outcome)
    if request_id:
        print("[ds-status-probe] request_id", request_id)
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