"""Helper de testes: materializa o submission snapshot antes de submeter.

A partir de 8.0.10.2, ``submit_aliexpress_order`` exige um
``AliExpressSubmissionSnapshot`` VÁLIDO (pre-flight canônico) antes de chamar
o provider. Estes helpers centralizam a materialização para os testes que
cobrem o fluxo happy-path/retry/reconciliação.
"""

from __future__ import annotations

import uuid


def materialize_snapshot(
    ali_order,
    *,
    logistics: str | None = "CAINIAO",
    session=None,
):
    """Materializa (UMA VEZ) o snapshot do pedido e devolve os itens de origem.

    ``logistics`` default "CAINIAO" simula o serviço escolhido; pode ser
    ``None`` para testar o caminho opcional.
    """
    from loja import db
    from loja.integrations.aliexpress.submission_snapshot import (
        materialize_submission_snapshot,
    )
    from loja.modules.models import AliExpressOrderItem

    sess = session or db.session
    item = sess.query(AliExpressOrderItem).filter_by(aliexpress_order_id=ali_order.id).first()
    mapping = {}
    if logistics is not None:
        mapping = {item.order_item_id: logistics}
    materialize_submission_snapshot(
        ali_order,
        logistics_service_by_order_item=mapping,
        session=sess,
    )
    db.session.commit()
    return ali_order
