"""Probe MANUAL remote-only de status DS AliExpress (Fase 8.0.7B).

Finalidade operacional:
    provar ``aliexpress.ds.trade.order.get`` com um order id remoto legítimo
    anterior ao fulfillment PolloModas, SEM fabricar ``AliExpressOrder`` local.

Uso explícito::

    ALIEXPRESS_REMOTE_ORDER_STATUS_PROBE=1 \
    python -m loja.integrations.aliexpress.ds_remote_status_probe \
        <supplier_id> <remote_order_id>

Garantias:
    * sem a flag: zero app, zero DB, zero HTTP;
    * valida argumentos antes de abrir app/DB;
    * exige Supplier real ``channel_type=aliexpress``;
    * credencial vem exclusivamente do credential_store pelo supplier;
    * exatamente uma chamada lógica ao adapter DS (sem retry/fallback);
    * nunca consulta/cria/altera AliExpressOrder/AliExpressOrderItem;
    * nunca chama placeorder, reconciliation, status_sync de domínio ou tracking;
    * rollback inclusive no sucesso para descartar qualquer flush incidental do
      credential_store (por exemplo marcação de credencial expirada);
    * saída sanitizada: nunca mostra remote_order_id, supplier_id, token, secret,
      ciphertext, URL assinada, query params, headers ou payload bruto.

Este probe NÃO ativa o adapter como default. ``get_default_order_status_syncer``
continua hard-off.
"""

from __future__ import annotations

import os
import sys
import uuid
from typing import Optional, Tuple

_PROBE_ENV = "ALIEXPRESS_REMOTE_ORDER_STATUS_PROBE"

_EXIT_CLOSED = 2
_EXIT_FAILURE = 1
_EXIT_SUCCESS = 0


def _probe_enabled() -> bool:
    return os.environ.get(_PROBE_ENV) == "1"


def _close_fail_closed() -> str:
    return (
        f"PROBE fechado: {_PROBE_ENV} != 1 (manual, remote-only, read-only). "
        "Nenhuma chamada HTTP ou de banco foi executada."
    )


def _validate_args(
    supplier_id: str, remote_order_id: str
) -> Optional[Tuple[uuid.UUID, str]]:
    """Validação sintática sem app/DB/HTTP."""
    try:
        supplier_uuid = uuid.UUID(str(supplier_id).strip())
    except (ValueError, TypeError, AttributeError):
        return None

    value = (remote_order_id or "").strip()
    if not value:
        return None
    return supplier_uuid, value


def run_probe(supplier_id: str, remote_order_id: str) -> int:
    """Executa exatamente uma consulta DS remote-only, sem persistência."""
    if not _probe_enabled():
        print(_close_fail_closed())
        return _EXIT_CLOSED

    parsed = _validate_args(supplier_id, remote_order_id)
    if parsed is None:
        print(
            "[ds-remote-status-probe] failure argumentos inválidos "
            "(supplier UUID e remote_order_id exigidos)"
        )
        return _EXIT_FAILURE
    supplier_uuid, value = parsed

    from loja import create_app

    app = create_app()
    with app.app_context():
        from loja import db
        from loja.modules.models import Supplier

        supplier = Supplier.query.filter_by(id=supplier_uuid).first()
        if supplier is None or supplier.channel_type != "aliexpress":
            db.session.rollback()
            print(
                "[ds-remote-status-probe] failure supplier AliExpress não encontrado"
            )
            return _EXIT_FAILURE
        if not getattr(supplier, "seller_id", None):
            db.session.rollback()
            print(
                "[ds-remote-status-probe] failure supplier AliExpress sem identidade remota"
            )
            return _EXIT_FAILURE

        from loja.integrations.aliexpress.ds_order_status import (
            DS_ORDER_STATUS_METHOD,
            AliExpressDSOrderStatusSyncer,
        )
        from loja.integrations.aliexpress.status_sync import (
            AliExpressOrderStatusRequest,
            AliExpressOrderStatusSyncError,
        )

        # Campos locais são sentinelas neutras e NÃO correspondem a rows locais.
        # O adapter DS usa somente supplier_id + ali_order_id para a consulta.
        sentinel = uuid.UUID(int=0)
        request = AliExpressOrderStatusRequest(
            aliexpress_order_id=sentinel,
            order_id=sentinel,
            supplier_id=supplier.id,
            ali_order_id=value,
            attempt_key=None,
        )
        syncer = AliExpressDSOrderStatusSyncer()

        try:
            result = syncer.sync(request)
        except AliExpressOrderStatusSyncError as exc:
            db.session.rollback()
            print("[ds-remote-status-probe] failure", str(exc))
            return _EXIT_FAILURE
        except Exception:
            db.session.rollback()
            print("[ds-remote-status-probe] failure erro interno (sem detalhes)")
            return _EXIT_FAILURE

        # Rollback obrigatório até no sucesso: este diagnóstico é read-only e
        # não preserva flush incidental do credential_store.
        db.session.rollback()
        print("[ds-remote-status-probe] success")
        print("[ds-remote-status-probe] method", DS_ORDER_STATUS_METHOD)
        print("[ds-remote-status-probe] outcome", result.outcome)
        if syncer.last_request_id:
            print("[ds-remote-status-probe] request_id", syncer.last_request_id)
        return _EXIT_SUCCESS


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not _probe_enabled():
        print(_close_fail_closed())
        return _EXIT_CLOSED
    if len(argv) != 2:
        print(
            "uso: python -m loja.integrations.aliexpress.ds_remote_status_probe "
            "<supplier_id> <remote_order_id>"
        )
        return _EXIT_FAILURE
    return run_probe(argv[0], argv[1])


if __name__ == "__main__":
    sys.exit(main())
