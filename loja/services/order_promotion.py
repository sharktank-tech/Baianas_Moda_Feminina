"""Item 1.3: promoção a "paid" com revalidação sob lock + rota de saída de
manual_review (aprovar/rejeitar) com auditoria.

- ``revalidate_and_promote_paid`` é o caminho ÚNICO de revalidação de
  estoque/variante/cupom sob lock e promoção para "paid"; o webhook de
  pagamento e a aprovação admin compartilham exatamente a mesma semântica.
- ``approve_manual_review``: apenas pedidos em ``manual_review``; bloqueia
  fail-closed quando a revalidação encontra motivo; grava a decisão na
  própria linha (migration 0032).
- ``reject_manual_review``: apenas pedidos em ``manual_review``; devolve a
  reserva via ``release_reserved_stock`` (o antigo ``_release_stock`` do
  webhook), cancela fulfillment pré-submissão e marca ``cancelled``.
"""

from dataclasses import dataclass
from typing import Sequence, Tuple

from flask import current_app

from loja import db
from loja.integrations.aliexpress import (
    cancel_fulfillment_before_submission,
    ensure_aliexpress_fulfillment,
)
from loja.modules.getway_mercado_pago.payment_models import Order
from loja.modules.models import Coupon, Product, ProductVariant
from loja.services.stock_reservations import release_reserved_stock
from loja.utils import utcnow

DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"

VALID_EXIT_STATUSES = {"manual_review"}
REJECTED_STATUS = "cancelled"


def product_uses_variants(product):
    """Um produto usa variantes quando vem do AliExpress ou possui variantes
    ativas com estoque. Mesma semântica do storefront (routes.py)."""
    variants = getattr(product, "variants", None) or []

    return (
        getattr(product, "source", None) == "aliexpress"
        or any(
            getattr(v, "is_active", False)
            and int(getattr(v, "stock", 0) or 0) > 0
            for v in variants
        )
    )


def revalidate_and_promote_paid(order) -> Sequence[str]:
    """Revalida estoque/variante/cupom sob lock e promove o pedido a "paid".

    Retorna a lista de motivos de bloqueio (vazia = promovido). Quando há
    motivos, NÃO escreve status: o chamador decide o que fazer (webhook vai
    para manual_review; a aprovação admin falha fechada e faz rollback).
    Os locks de produto/variante/cupom são adquiridos aqui — mesma ordem do
    caminho original do webhook.
    """
    locked_products = {}
    locked_variants = {}
    review_reasons = []

    for item in order.items:
        if item.product_id is None:
            review_reasons.append(
                f"Produto removido: {item.product_name}."
            )
            continue

        product = (
            Product.query
            .filter_by(id=item.product_id)
            .with_for_update()
            .first()
        )
        locked_products[item.id] = product

        if product is None:
            review_reasons.append(
                f"Produto não encontrado: {item.product_name}."
            )
        elif (
            not order.stock_reserved
            and product.stock is not None
            and product.stock < item.quantity
        ):
            review_reasons.append(
                f"Estoque insuficiente: {item.product_name}."
            )

        if (
            product is not None
            and product_uses_variants(product)
            and item.variant_id is None
        ):
            # Integridade: produto que usa variantes (ex.: AliExpress)
            # com OrderItem sem variant_id. NUNCA debitamos Product.stock
            # isoladamente aqui — o pagamento vai para revisão manual com
            # mensagem sanitizada (sem supplier_sku_id/external_sku_id).
            review_reasons.append(
                f"Variação não informada: {item.product_name}."
            )

        if item.variant_id is not None:
            variant = (
                ProductVariant.query
                .filter_by(id=item.variant_id, product_id=item.product_id)
                .with_for_update()
                .first()
            )
            locked_variants[item.id] = variant

            # Integridade Product<->Variant: a variante deve existir E
            # pertencer ao produto do item — validada SEMPRE,
            # independentemente de stock_reserved. Uma variante
            # inexistente ou de outro produto é erro de integridade e
            # nunca deve virar "paid" silenciosamente.
            if variant is None:
                review_reasons.append(
                    f"Variação inválida: {item.product_name}."
                )
            elif not order.stock_reserved and (
                not variant.is_active
                or int(variant.stock or 0) < item.quantity
            ):
                # Estoque/disponibilidade só importam quando um novo
                # débito será necessário (reserva já abatida dispensa).
                review_reasons.append(
                    f"Estoque insuficiente: {item.product_name}."
                )

    locked_coupon = None
    if order.coupon_id and order.coupon_counted_at is None:
        locked_coupon = (
            Coupon.query
            .filter_by(id=order.coupon_id)
            .with_for_update()
            .first()
        )
        if (
            locked_coupon is not None
            and locked_coupon.usage_limit is not None
            and locked_coupon.times_used >= locked_coupon.usage_limit
        ):
            review_reasons.append(
                f"Limite do cupom {order.coupon_code} atingido."
            )

    if review_reasons:
        return review_reasons

    # Pedidos com reserva já tiveram o estoque abatido na criação.
    if not order.stock_reserved:
        for item in order.items:
            product = locked_products.get(item.id)
            if product is not None and product.stock is not None:
                product.stock -= item.quantity

            if item.variant_id is not None:
                variant = locked_variants.get(item.id)
                if (
                    variant is not None
                    and variant.stock is not None
                ):
                    variant.stock -= item.quantity

        # Pagamento aprovado tardio: o estoque foi debitado agora e
        # precisa poder ser restituído em um eventual refund.
        order.stock_reserved = True

    if locked_coupon is not None:
        locked_coupon.times_used += 1
        order.coupon_counted_at = utcnow()

    order.status = "paid"
    order.manual_review_reason = None

    # Fulfillment AliExpress (Fase 8.0) — idempotente, mesma transação.
    # Só roda para pedidos com status "paid" (não manual_review).
    ensure_aliexpress_fulfillment(order)
    return []


@dataclass(frozen=True)
class ManualReviewResult:
    """Resultado de approve/reject: outcome em {changed, blocked, noop}."""

    ok: bool
    action: str
    outcome: str
    reasons: Tuple[str, ...] = ()


def _log(action, order_id, admin_user_id, outcome, reasons=()):
    current_app.logger.info(
        "manual_review_decision action=%s order=%s admin=%s outcome=%s "
        "reasons=%s",
        action,
        order_id,
        admin_user_id,
        outcome,
        ",".join(str(r) for r in reasons)[:300],
    )


def _locked_order(order_id):
    return (
        Order.query
        .filter_by(id=order_id)
        .with_for_update(of=Order)
        .first()
    )


def approve_manual_review(order_id, *, admin_user_id, note=None):
    """Aprova um manual_review: revalida sob lock e promove a "paid".

    Fail-closed: qualquer motivo de revalidação bloqueia sem alterar nada;
    a decisão (se ocorrer) fica gravada na própria linha do pedido.
    """
    note = str(note or "").strip()[:500] or None
    try:
        order = _locked_order(order_id)
        if order is None or order.status not in VALID_EXIT_STATUSES:
            db.session.rollback()
            _log("approve", order_id, admin_user_id, "noop",
                 ("not_manual_review",))
            return ManualReviewResult(
                False, "approve", "noop", ("not_manual_review",)
            )

        order.paid_at = order.paid_at or utcnow()
        blocking = revalidate_and_promote_paid(order)
        if blocking:
            db.session.rollback()
            _log("approve", order_id, admin_user_id, "blocked", blocking)
            return ManualReviewResult(
                False, "approve", "blocked", tuple(blocking)
            )

        order.manual_review_decision = DECISION_APPROVED
        order.manual_review_decided_at = utcnow()
        order.manual_review_decided_by_user_id = admin_user_id
        order.manual_review_decided_reason = note
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    _log("approve", order_id, admin_user_id, "changed")
    return ManualReviewResult(True, "approve", "changed")


def reject_manual_review(order_id, *, admin_user_id, reason):
    """Rejeita um manual_review: devolve a reserva e marca "cancelled".

    O motivo é obrigatório (auditoria). O estoque é restituído pelo mesmo
    caminho do webhook (release_reserved_stock); sem reserva, nada muda.
    """
    reason = str(reason or "").strip()[:500]
    if not reason:
        return ManualReviewResult(
            False, "reject", "blocked", ("reason_required",)
        )

    try:
        order = _locked_order(order_id)
        if order is None or order.status not in VALID_EXIT_STATUSES:
            db.session.rollback()
            _log("reject", order_id, admin_user_id, "noop",
                 ("not_manual_review",))
            return ManualReviewResult(
                False, "reject", "noop", ("not_manual_review",)
            )

        release_reserved_stock(order)
        order.status = REJECTED_STATUS
        order.manual_review_decision = DECISION_REJECTED
        order.manual_review_decided_at = utcnow()
        order.manual_review_decided_by_user_id = admin_user_id
        order.manual_review_decided_reason = reason
        cancel_fulfillment_before_submission(order)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    _log("reject", order_id, admin_user_id, "changed")
    return ManualReviewResult(True, "reject", "changed")
