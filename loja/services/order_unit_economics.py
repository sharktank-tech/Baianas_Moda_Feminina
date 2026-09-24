"""FASE 8.2.0-C — adapter read-only de unit economics para Order.

Traduz os fatos monetários persistidos em um pedido para o evaluator canônico.
O adapter não consulta catálogo, fornecedor ou provider e não persiste nada.

O landed cost precisa vir de uma fonte canônica congelada pelo caller. Quando
ausente, a avaliação fica incompleta; ele nunca é inferido do catálogo atual.

Contrato de evidência de pagamento (``evaluate_order_unit_economics_with_payment``):
1. elegibilidade e consistência do Payment com o Order (vínculo, valor, moeda e
   payload) antes de qualquer caminho financeiro;
2. retenção observada tem prioridade;
3. na ausência, o cenário da modalidade (``raw_payload.installments``);
4. sem evidência utilizável, os custos de pagamento ficam ``unknown``
   (fail-closed) — nunca aditivo e nunca zero.
Evidência contraditória (valor/moeda/payload incompatível) é rejeição
explícita (``OrderUnitEconomicsError``); informação ausente é incompleta.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Optional

from .payment_net_receipt import PaymentNetReceiptError, resolve_payment_net_receipt
from .payment_scenario import (
    MERCADO_PAGO_PROVIDER,
    resolve_payment_scenario,
)
from .unit_economics import UnitEconomicsResult, evaluate_unit_economics
from .unit_economics_policy import (
    PAYMENT_SCENARIO_CASH,
    UnitEconomicsCostPolicy,
    build_unit_economics_components,
    load_unit_economics_cost_policy,
)


class OrderUnitEconomicsError(ValueError):
    """Fato monetário inválido ou inconsistente no pedido."""


@dataclass(frozen=True)
class OrderUnitEconomicsResult:
    complete: bool
    gross_revenue: Decimal
    charged_amount: Decimal
    discount_amount: Decimal
    unknown_components: tuple[str, ...]
    evaluation: Optional[UnitEconomicsResult]

    @property
    def profitable(self) -> bool:
        return bool(self.evaluation is not None and self.evaluation.profitable)


def _money(value, field: str) -> Decimal:
    if value is None:
        raise OrderUnitEconomicsError(f"{field}_missing")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise InvalidOperation
        amount = amount.quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise OrderUnitEconomicsError(f"{field}_invalid") from None
    if amount < 0:
        raise OrderUnitEconomicsError(f"{field}_negative")
    return amount


def _policy_unknowns(
    policy: UnitEconomicsCostPolicy,
    *,
    processor_retention_known: bool = False,
    payment_scenario: Optional[str] = None,
) -> tuple[str, ...]:
    if processor_retention_known:
        payment_fields: tuple[tuple[str, object], ...] = ()
    elif payment_scenario is not None:
        percent = (
            policy.gateway_fee_percent
            if payment_scenario == PAYMENT_SCENARIO_CASH
            else policy.installment_cost_percent
        )
        payment_fields = () if percent is not None else (("payment_fee", percent),)
    else:
        payment_fields = (
            ("gateway_fee", policy.gateway_fee_percent),
            ("installment_cost", policy.installment_cost_percent),
        )
    fields = (
        *payment_fields,
        ("tax", policy.tax_percent),
        ("acquisition_cost", policy.acquisition_cost_per_order),
        ("refund_reserve", policy.refund_reserve_percent),
    )
    return tuple(name for name, value in fields if value is None)


def evaluate_order_unit_economics(
    order,
    *,
    landed_cost=None,
    policy: Optional[UnitEconomicsCostPolicy] = None,
    processor_retained_amount=None,
    payment_scenario: Optional[str] = None,
) -> OrderUnitEconomicsResult:
    """Avalia um Order sem mutá-lo e sem consultar fontes externas.

    Contrato:
    - receita bruta = subtotal + shipping;
    - valor cobrado = total;
    - desconto é contado exatamente uma vez como custo variável;
    - total deve ser igual a receita bruta - desconto;
    - landed_cost ausente mantém o resultado incompleto e fail-closed;
    - ``payment_scenario`` (quando informado) seleciona a taxa total da
      modalidade; sem ele e sem retenção, o construtor mantém o contrato
      aditivo legado (chamadores com evidência devem usar
      ``evaluate_order_unit_economics_with_payment``).
    """
    subtotal = _money(getattr(order, "subtotal", None), "order_subtotal")
    shipping = _money(getattr(order, "shipping", None), "order_shipping")
    discount = _money(getattr(order, "discount", None), "order_discount")
    charged = _money(getattr(order, "total", None), "order_total")

    gross = (subtotal + shipping).quantize(Decimal("0.01"))
    if discount > gross:
        raise OrderUnitEconomicsError("order_discount_exceeds_gross_revenue")
    if charged != (gross - discount).quantize(Decimal("0.01")):
        raise OrderUnitEconomicsError("order_total_mismatch")

    resolved_policy = policy or load_unit_economics_cost_policy()
    variable, operational = build_unit_economics_components(
        charged_amount=charged,
        discount_amount=discount,
        policy=resolved_policy,
        processor_retained_amount=processor_retained_amount,
        payment_scenario=payment_scenario,
    )

    if landed_cost is None:
        return OrderUnitEconomicsResult(
            complete=False,
            gross_revenue=gross,
            charged_amount=charged,
            discount_amount=discount,
            unknown_components=(
                "landed_cost",
                *_policy_unknowns(
                    resolved_policy,
                    processor_retention_known=(
                        processor_retained_amount is not None
                    ),
                    payment_scenario=payment_scenario,
                ),
            ),
            evaluation=None,
        )

    landed = _money(landed_cost, "landed_cost")
    if landed <= 0:
        raise OrderUnitEconomicsError("landed_cost_invalid")

    evaluation = evaluate_unit_economics(
        sale_price=gross,
        landed_cost=landed,
        variable_costs=variable,
        operational_costs=operational,
    )
    return OrderUnitEconomicsResult(
        complete=evaluation.complete,
        gross_revenue=gross,
        charged_amount=charged,
        discount_amount=discount,
        unknown_components=evaluation.unknown_components,
        evaluation=evaluation,
    )



def resolve_order_landed_cost_snapshot(order) -> Optional[Decimal]:
    """Soma o landed cost congelado dos itens, sem consultar o catálogo.

    Retorna None quando qualquer item possui snapshot incompleto (incluindo
    pedidos legados). Valores presentes porém inválidos ou incoerentes são
    erros de integridade e falham fechado.
    """
    items = list(getattr(order, "items", None) or [])
    if not items:
        raise OrderUnitEconomicsError("order_items_missing")

    total = Decimal("0.00")
    currency = None

    for item in items:
        raw_quantity = getattr(item, "quantity", None)
        try:
            quantity = Decimal(str(raw_quantity))
        except (InvalidOperation, TypeError, ValueError):
            raise OrderUnitEconomicsError("order_item_quantity_invalid") from None
        if not quantity.is_finite() or quantity < 1 or quantity != quantity.to_integral_value():
            raise OrderUnitEconomicsError("order_item_quantity_invalid")

        raw_unit = getattr(item, "supplier_unit_cost_snapshot", None)
        raw_shipping = getattr(item, "supplier_shipping_cost_snapshot", None)
        raw_landed = getattr(item, "supplier_landed_cost_snapshot", None)
        raw_currency = getattr(item, "supplier_cost_currency_snapshot", None)

        if (
            raw_unit is None
            or raw_shipping is None
            or raw_landed is None
            or not raw_currency
        ):
            return None

        unit = _money(raw_unit, "order_item_supplier_unit_cost_snapshot")
        shipping = _money(
            raw_shipping,
            "order_item_supplier_shipping_cost_snapshot",
        )
        landed = _money(raw_landed, "order_item_supplier_landed_cost_snapshot")
        if unit <= 0 or landed <= 0:
            raise OrderUnitEconomicsError("order_item_landed_cost_invalid")
        if landed != (unit + shipping).quantize(Decimal("0.01")):
            raise OrderUnitEconomicsError("order_item_landed_cost_mismatch")

        item_currency = str(raw_currency).strip().upper()
        if len(item_currency) != 3:
            raise OrderUnitEconomicsError("order_item_cost_currency_invalid")
        if currency is None:
            currency = item_currency
        elif item_currency != currency:
            raise OrderUnitEconomicsError("order_item_cost_currency_mismatch")

        total += landed * quantity

    return total.quantize(Decimal("0.01"))


def evaluate_order_unit_economics_from_snapshot(
    order,
    *,
    policy: Optional[UnitEconomicsCostPolicy] = None,
) -> OrderUnitEconomicsResult:
    """Avalia exclusivamente fatos persistidos no pedido e em seus itens."""
    return evaluate_order_unit_economics(
        order,
        landed_cost=resolve_order_landed_cost_snapshot(order),
        policy=policy,
    )



def _payload_currency(payment) -> Optional[str]:
    payload = getattr(payment, "raw_payload", None)
    if not isinstance(payload, dict):
        return None
    raw = payload.get("currency_id")
    if raw is None or raw == "":
        return None
    return str(raw).strip().upper()


def _payload_transaction_amount(payment) -> Optional[Decimal]:
    payload = getattr(payment, "raw_payload", None)
    if not isinstance(payload, dict):
        return None
    raw = payload.get("transaction_amount")
    if raw is None:
        return None
    return _money(raw, "payload_transaction_amount")


def _is_approved_mp_payment(payment) -> bool:
    provider = str(getattr(payment, "provider", "") or "").strip()
    status = str(getattr(payment, "status", "") or "").strip().lower()
    return provider == MERCADO_PAGO_PROVIDER and status == "approved"


def _validate_payment_link_and_amount(order, payment) -> Decimal:
    """Vínculo e valor cobrado: contradição é erro explícito."""
    order_id = getattr(order, "id", None)
    payment_order_id = getattr(payment, "order_id", None)
    if (
        order_id is not None
        and payment_order_id is not None
        and str(order_id) != str(payment_order_id)
    ):
        raise OrderUnitEconomicsError("payment_order_mismatch")

    charged = _money(getattr(order, "total", None), "order_total")
    payment_amount_raw = getattr(payment, "amount", None)
    if payment_amount_raw is None:
        raise OrderUnitEconomicsError("payment_amount_missing")
    payment_amount = _money(payment_amount_raw, "payment_amount")
    if payment_amount != charged:
        raise OrderUnitEconomicsError("payment_charged_amount_mismatch")
    return charged


def _validate_payload_consistency(payment, payment_amount: Decimal) -> None:
    """Campos presentes no payload precisam bater com o Payment persistido."""
    payload = getattr(payment, "raw_payload", None)
    if not isinstance(payload, dict):
        return

    payload_amount = _payload_transaction_amount(payment)
    if payload_amount is not None and payload_amount != payment_amount:
        raise OrderUnitEconomicsError("payment_payload_amount_mismatch")

    payload_currency = _payload_currency(payment)
    if payload_currency is not None:
        persisted_currency = str(
            getattr(payment, "currency", "") or ""
        ).strip().upper()
        if (
            len(persisted_currency) != 3
            or len(payload_currency) != 3
            or payload_currency != persisted_currency
        ):
            raise OrderUnitEconomicsError("payment_payload_currency_mismatch")


def evaluate_order_unit_economics_with_payment(
    order,
    payment,
    *,
    policy: Optional[UnitEconomicsCostPolicy] = None,
) -> OrderUnitEconomicsResult:
    """Evidência de pagamento em precedência: elegibilidade → retenção → cenário → desconhecido.

    O Payment e o Order precisam representar a mesma cobrança. Nenhum dado é
    buscado no provider e nenhum objeto é mutado.

    - vínculo/valor/payload inconsistentes → ``OrderUnitEconomicsError``
      (rejeição explícita; distingue contradição de ausência);
    - Payment não elegível (provider/status) → sem evidência utilizável
      (avaliação incompleta, não erro);
    - retenção observada (``resolve_payment_net_receipt``) substitui as
      estimativas;
    - sem retenção, o cenário da modalidade (``resolve_payment_scenario``)
      seleciona a taxa total correspondente;
    - sem nenhuma evidência (inclusive ``payment=None``), os campos de
      pagamento da política viram ``None`` e os componentes ficam
      ``unknown``: a avaliação fica incompleta. O aditivo legado (somar
      gateway + parcelamento) NÃO é usado neste caminho.
    """
    active_policy = policy or load_unit_economics_cost_policy()
    retained = None
    scenario = None

    if payment is not None:
        charged = _validate_payment_link_and_amount(order, payment)
        _validate_payload_consistency(payment, charged)

        if _is_approved_mp_payment(payment):
            try:
                receipt = resolve_payment_net_receipt(payment)
            except PaymentNetReceiptError as exc:
                raise OrderUnitEconomicsError(str(exc)) from None
            if receipt is not None:
                if receipt.charged_amount != charged:
                    raise OrderUnitEconomicsError(
                        "payment_charged_amount_mismatch"
                    )
                retained = receipt.processor_retained_amount
            else:
                scenario = resolve_payment_scenario(payment)
        # provider/status não elegíveis → sem evidência (incompleto)

    if retained is None and scenario is None:
        active_policy = replace(
            active_policy,
            gateway_fee_percent=None,
            installment_cost_percent=None,
        )

    return evaluate_order_unit_economics(
        order,
        landed_cost=resolve_order_landed_cost_snapshot(order),
        policy=active_policy,
        processor_retained_amount=retained,
        payment_scenario=scenario,
    )


__all__ = [
    "OrderUnitEconomicsError",
    "OrderUnitEconomicsResult",
    "evaluate_order_unit_economics",
    "evaluate_order_unit_economics_from_snapshot",
    "evaluate_order_unit_economics_with_payment",
    "resolve_order_landed_cost_snapshot",
]
