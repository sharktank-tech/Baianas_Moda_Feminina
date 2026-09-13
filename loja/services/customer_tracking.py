"""Visão sanitizada de rastreio para o cliente (FASE 8.3.0-C).

Somente dados já persistidos e validados são expostos. IDs do fornecedor,
attempt keys, payloads e diagnósticos internos nunca fazem parte da projeção.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple


_STATUS_LABELS = {
    "queued": "Pedido recebido",
    "blocked": "Aguardando revisão",
    "ready": "Preparando envio",
    "submitting": "Preparando envio",
    "submitted": "Pedido enviado ao fornecedor",
    "confirmed": "Envio confirmado",
    "shipped": "Em trânsito",
    "delivered": "Entregue",
    "cancel_requested": "Cancelamento solicitado",
    "cancelled": "Cancelado",
    "failed": "Aguardando revisão",
    "manual_review": "Aguardando revisão",
    "pending": "Processando",
    "pending_reconciliation": "Confirmando pedido",
}


@dataclass(frozen=True)
class CustomerTrackingRow:
    status: str
    status_label: str
    tracking_code: Optional[str]
    carrier: Optional[str]
    updated_at: Optional[datetime]


def build_customer_tracking(order) -> Tuple[CustomerTrackingRow, ...]:
    """Projeta fulfillment 0/1/N sem expor identidade remota."""
    rows = []
    fulfillments = sorted(
        tuple(getattr(order, "aliexpress_orders", None) or ()),
        key=lambda item: (getattr(item, "created_at", None) is None,
                          getattr(item, "created_at", None)),
    )
    for fulfillment in fulfillments:
        state = str(getattr(fulfillment, "state", "") or "")
        label = _STATUS_LABELS.get(state, "Processando")
        remotes = sorted(
            tuple(getattr(fulfillment, "remote_orders", None) or ()),
            key=lambda item: getattr(item, "position", 0),
        )
        if not remotes:
            rows.append(CustomerTrackingRow(state, label, None, None, None))
            continue
        for remote in remotes:
            rows.append(
                CustomerTrackingRow(
                    status=state,
                    status_label=label,
                    tracking_code=getattr(remote, "tracking_code", None),
                    carrier=getattr(remote, "carrier", None),
                    updated_at=getattr(remote, "tracking_updated_at", None),
                )
            )
    return tuple(rows)
