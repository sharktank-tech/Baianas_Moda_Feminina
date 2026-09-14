"""FASE 9.0.0-B — métricas por canal (somente leitura, fail-closed).

Agrega pedidos em grupos por origem de tráfego capturada no checkout
(utm_source preferido; fallback referrer; senão "direto"). Nenhuma métrica é
inventada: sem sessão de navegação persistida não existe "sessões" e sem dados
de investimento publicitário não existe CAC — esses campos permanecem ausentes.

A margem por pedido reutiliza o avaliador canônico congelado (FASE 8.2.0-C);
pedidos com snapshot de custo incompleto contribuem com margem ``None`` e são
contabilizados em ``margin_known``. Nunca consulta provider e nunca muta nada.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional

from ..modules.getway_mercado_pago.payment_models import Order
from .order_unit_economics import (
    OrderUnitEconomicsError,
    evaluate_order_unit_economics_from_snapshot,
)
from .unit_economics_policy import load_unit_economics_cost_policy


@dataclass(frozen=True)
class ChannelMetricRow:
    channel: str
    sessions: int
    orders: int
    paid_orders: int
    conversion: Optional[Decimal]
    revenue: Optional[Decimal]
    avg_ticket: Optional[Decimal]
    margin_known: int
    total_margin: Optional[Decimal]
    avg_margin: Optional[Decimal]
    repeat_customers: int


@dataclass(frozen=True)
class ChannelMetricsTotals:
    sessions: int
    orders: int
    paid_orders: int
    revenue: Optional[Decimal]
    margin_known_orders: int
    margin_known_value: Optional[Decimal]


def _is_paid(order) -> bool:
    return getattr(order, "status", None) == "paid"


def _channel_of(order) -> str:
    source = getattr(order, "acquisition_source", None)
    if source:
        return source
    referrer = getattr(order, "acquisition_referrer", None)
    if referrer:
        return f"referrer:{referrer}"
    return "direto"


def _positive_dec(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except Exception:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount.quantize(Decimal("0.01"))


def _quant2(value) -> Decimal:
    return value.quantize(Decimal("0.01"))


def build_channel_metrics(
    orders: Iterable[Order],
    *,
    policy=None,
    session_counts: Optional[dict[str, int]] = None,
) -> tuple[list[ChannelMetricRow], ChannelMetricsTotals]:
    """Agrega pedidos por canal de forma read-only e sanitizada.

    ``policy`` opcional permite injeção determinística em testes; por padrão
    carrega a política canônica. Exceções de avaliação de um pedido nunca
    derrubam a agregação: o pedido entra como margem desconhecida.
    """
    active_policy = policy if policy is not None else load_unit_economics_cost_policy()
    rows: dict[str, dict] = {}
    known_sessions = session_counts or {}

    def bucket(channel: str) -> dict:
        if channel not in rows:
            rows[channel] = {
                "sessions": max(0, int(known_sessions.get(channel, 0))),
                "orders": 0,
                "paid_orders": 0,
                "revenue": Decimal("0.00"),
                "margin_known": 0,
                "margin_sum": Decimal("0.00"),
                "paid_customers": set(),
            }
        return rows[channel]

    for channel in known_sessions:
        bucket(channel)

    for order in orders:
        channel = _channel_of(order)
        group = bucket(channel)
        group["orders"] += 1

        if _is_paid(order):
            group["paid_orders"] += 1
            amount = _positive_dec(getattr(order, "total", None))
            if amount is not None:
                group["revenue"] += amount
            email = getattr(order, "customer_email", None)
            if email:
                group["paid_customers"].add(email)

        try:
            result = evaluate_order_unit_economics_from_snapshot(
                order,
                policy=active_policy,
            )
            if result.complete and result.evaluation is not None:
                margin = result.evaluation.operational_margin
                if margin is not None and margin.is_finite():
                    group["margin_known"] += 1
                    group["margin_sum"] += margin
        except OrderUnitEconomicsError:
            continue
        except (TypeError, ValueError):
            continue

    result_rows: list[ChannelMetricRow] = []
    for channel, group in sorted(rows.items()):
        orders_n = group["orders"]
        paid_n = group["paid_orders"]
        revenue = (
            _quant2(group["revenue"]) if group["paid_orders"] else None
        )
        sessions_n = group["sessions"]
        conversion = (
            _quant2(Decimal(str(paid_n)) / Decimal(str(sessions_n)))
            if sessions_n
            else None
        )
        avg_ticket = (
            _quant2(revenue / Decimal(str(paid_n)))
            if paid_n and revenue is not None
            else None
        )
        total_margin = (
            _quant2(group["margin_sum"]) if group["margin_known"] else None
        )
        avg_margin = (
            _quant2(group["margin_sum"] / Decimal(str(group["margin_known"])))
            if group["margin_known"]
            else None
        )
        result_rows.append(
            ChannelMetricRow(
                channel=channel,
                sessions=sessions_n,
                orders=orders_n,
                paid_orders=paid_n,
                conversion=conversion,
                revenue=revenue,
                avg_ticket=avg_ticket,
                margin_known=group["margin_known"],
                total_margin=total_margin,
                avg_margin=avg_margin,
                repeat_customers=len(group["paid_customers"]),
            )
        )

    totals = ChannelMetricsTotals(
        sessions=sum(group["sessions"] for group in rows.values()),
        orders=sum(group["orders"] for group in rows.values()),
        paid_orders=sum(group["paid_orders"] for group in rows.values()),
        revenue=(
            _quant2(sum(group["revenue"] for group in rows.values()))
            if sum(group["paid_orders"] for group in rows.values())
            else None
        ),
        margin_known_orders=sum(
            group["margin_known"] for group in rows.values()
        ),
        margin_known_value=(
            _quant2(sum(group["margin_sum"] for group in rows.values()))
            if sum(group["margin_known"] for group in rows.values())
            else None
        ),
    )

    return result_rows, totals


__all__ = [
    "ChannelMetricRow",
    "ChannelMetricsTotals",
    "build_channel_metrics",
]