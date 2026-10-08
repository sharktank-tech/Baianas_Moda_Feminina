"""Liberação idempotente de reservas com expiração comprovada.

A rotina nunca consulta nem cancela recursos no Mercado Pago. Somente pedidos
com prazo persistido, já vencido, ainda pending_payment e sem pagamento
canônico aprovado podem devolver estoque. Pedidos legados sem prazo falham
fechados.

A devolução NUNCA infla o estoque acima do que o fornecedor ainda informa:
quando `ProductVariant.supplier_stock` (espelho gravado pela sincronização
técnica) é conhecido, o teto é `supplier_stock - outras reservas` — se o
fornecedor reduziu o estoque depois da sync, o pedido vencido devolve só o
que cabe nesse teto. Sem espelho (variante nunca sincronizada) mantém-se o
comportamento histórico de devolver a quantidade integral.

A devolução à variante DESABILITADA comercialmente restaura o estoque DELA
(com o teto acima), mas NÃO entra no agregado `Product.stock`: o agregado é
a soma canônica das variantes ATIVAS, e somar a restituição de uma variante
fora de venda o inflaria e divergiria da soma. A reativação explícita no
admin recalcula o agregado do zero, incorporando o que foi devolvido.
"""

from dataclasses import dataclass

from flask import current_app
from sqlalchemy import func, select

from loja import db
from loja.modules.getway_mercado_pago.payment_models import (
    Order,
    OrderItem,
    Payment,
)
from loja.modules.models import AliExpressOrder, Product, ProductVariant
from loja.services.reservation_policy import (
    DEFAULT_RESERVATION_HOURS,
    load_reservation_hours,
)
from loja.utils import utcnow

DEFAULT_RELEASE_LIMIT = 100
MAX_RELEASE_LIMIT = 500


@dataclass(frozen=True)
class StockReservationReleaseResult:
    candidates: int
    released: int
    skipped: int
    hours: int
    source: str


def _bounded_int(value, *, default, minimum, maximum, code):
    if value is None or str(value).strip() == "":
        return default
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(code) from None
    if parsed < minimum or parsed > maximum:
        raise ValueError(code)
    return parsed


def load_release_limit(value=None):
    if value is None:
        value = current_app.config.get(
            "STOCK_RESERVATION_RELEASE_LIMIT",
            DEFAULT_RELEASE_LIMIT,
        )
    return _bounded_int(
        value,
        default=DEFAULT_RELEASE_LIMIT,
        minimum=1,
        maximum=MAX_RELEASE_LIMIT,
        code="stock_reservation_release_limit_invalid",
    )


def is_scheduled_release_enabled():
    return bool(
        current_app.config.get("STOCK_RESERVATION_RELEASE_ENABLED", False)
    )


def _latest_payment(order_id):
    return (
        Payment.query
        .filter_by(order_id=order_id)
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .first()
    )


def _latest_payments_by_order(order_ids):
    """Último pagamento por pedido em uma única consulta (sem N+1).

    Preserva exatamente a política latest-first: ORDER BY order_id,
    created_at DESC, id DESC — a primeira linha de cada pedido vence,
    com empate de created_at resolvido pelo maior id.
    """
    ids = list(dict.fromkeys(order_ids))
    if not ids:
        return {}
    rows = (
        Payment.query
        .filter(Payment.order_id.in_(ids))
        .order_by(
            Payment.order_id,
            Payment.created_at.desc(),
            Payment.id.desc(),
        )
        .all()
    )
    latest = {}
    for payment in rows:
        if payment.order_id not in latest:
            latest[payment.order_id] = payment
    return latest


def _release_eligible(order, *, now):
    if (
        order is None
        or not order.stock_reserved
        or order.status != "pending_payment"
        or order.mp_preference_expires_at is None
        or order.mp_preference_expires_at >= now
    ):
        return False
    payment = _latest_payment(order.id)
    return payment is None or payment.status != "approved"


def find_active_stock_reservations(*, limit=None, now=None):
    """Lista reservas ativas com expiração comprovada futura, sem mutação.

    Critério (somente leitura): status pending_payment, reserva ainda ativa
    (``stock_reserved`` verdadeiro — não existe coluna de timestamp de
    reserva/liberação, e nenhuma migração é criada aqui), expiração da
    preferência persistida e ainda vigente (``>= now``: expiração exatamente
    igual a now conta como ativa e nunca é liberável), e nenhum pagamento
    canônico aprovado (política latest-first). Pedidos legados sem
    expiração (NULL) nunca entram aqui.
    """
    limit = load_release_limit(limit)
    effective_now = now or utcnow()
    candidates = (
        Order.query
        .filter(
            Order.stock_reserved.is_(True),
            Order.status == "pending_payment",
            Order.mp_preference_expires_at.isnot(None),
            Order.mp_preference_expires_at >= effective_now,
        )
        .order_by(Order.mp_preference_expires_at.asc(), Order.id.asc())
        .limit(limit)
        .all()
    )
    latest_by_order = _latest_payments_by_order([o.id for o in candidates])
    eligible = []
    for order in candidates:
        payment = latest_by_order.get(order.id)
        if payment is None or payment.status != "approved":
            eligible.append(order)
    return eligible


def reserved_quantities_by_variant(variant_ids):
    """Quantidades ainda debitadas do estoque LOCAL por variante.

    Contabilidade canônica para qualquer rotina que RECALCULE estoque
    absoluto a partir do fornecedor (sincronização técnica do produto
    publicado): toda linha com ``stock_reserved`` verdadeiro continua
    debitada aqui, e por isso NUNCA pode ser devolvida por um valor
    absoluto vindo do fornecedor. Pedido cujo fulfillment já foi SUBMETIDO
    ao fornecedor é excluído: nesse caso o abatimento já consta no estoque
    reportado pelo fornecedor e subtrair de novo duplicaria a baixa.

    Somente leitura e sem lock. Quem recalcula absolutos trava produto e
    variantes na mesma ordem do checkout (Product -> ProductVariant).
    """
    ids = {value for value in (variant_ids or []) if value is not None}
    if not ids:
        return {}

    # `order_id` anulável: um registro submetido SEM pedido vinculado não
    # pode virar NULL no conjunto (NOT IN com NULL anularia TODAS as
    # reservas — a sync voltaria a devolver estoque já debitado).
    submitted_orders = select(AliExpressOrder.order_id).where(
        AliExpressOrder.submitted_at.isnot(None),
        AliExpressOrder.order_id.isnot(None),
    )
    rows = (
        db.session.query(OrderItem.variant_id, func.sum(OrderItem.quantity))
        .join(Order, Order.id == OrderItem.order_id)
        .filter(
            Order.stock_reserved.is_(True),
            Order.id.not_in(submitted_orders),
            OrderItem.variant_id.in_(ids),
        )
        .group_by(OrderItem.variant_id)
        .all()
    )
    return {
        variant_id: int(total or 0)
        for variant_id, total in rows
        if variant_id is not None
    }


def _own_reserved_by_variant(order):
    """Reserva deste pedido por variante (itens sem variante ficam fora)."""
    totals = {}
    for item in order.items:
        if item.variant_id is None:
            continue
        totals[item.variant_id] = totals.get(item.variant_id, 0) + int(
            item.quantity or 0
        )
    return totals


def _restock_variant(variant, quantity, *, others_reserved):
    """Devolve ``quantity`` à variante COM teto no estoque do fornecedor.

    O espelho ``ProductVariant.supplier_stock`` (escrito pela sincronização
    técnica) limita a restituição a
    ``supplier_stock - outras reservas ainda debitadas``: quando o
    fornecedor REDUZIU o estoque depois da última sync, a liberação nunca
    infla o estoque local acima dele. Sem espelho (nunca sincronizado) o
    comportamento histórico (``estoque + quantidade``) é preservado.
    Devolve o delta efetivamente aplicado à variante.
    """
    before = int(variant.stock or 0)
    target = before + int(quantity)
    supplier_stock = variant.supplier_stock
    if supplier_stock is not None:
        ceiling = max(int(supplier_stock) - int(others_reserved), 0)
        target = min(target, ceiling)
    variant.stock = target
    return target - before


def _others_reserved_for(variant, own_quantity):
    """Reservas de OUTROS pedidos sobre a variante (sob a leitura atual)."""
    total = reserved_quantities_by_variant([variant.id]).get(variant.id, 0)
    return max(total - own_quantity, 0)


def _apply_release_delta(product, variant, delta):
    """Reflete a restituição no agregado sem inflar a vitrine.

    `Product.stock` é a soma canônica das variantes ATIVAS: quando a
    variante está desabilitada comercialmente, a devolução restaura o
    estoque DELA mas não entra no agregado. Itens sem variante (legado,
    sem conceito de disponibilidade) mantêm o comportamento histórico.
    """
    if product.stock is None:
        return
    if variant is not None and not variant.is_active:
        return
    product.stock += delta


def count_unproven_stock_reservations():
    """Contagem fail-closed de reservas sem expiração comprovada.

    Pedidos legados (``mp_preference_expires_at`` NULL) com reserva ativa
    não são classificáveis: não entram nem em ativas nem em vencidas, e
    nenhuma ação é oferecida para eles. Pedidos cujo pagamento latest-first
    já está aprovado saem da contagem (pagamento decide, não a ausência
    de prazo).
    """
    candidate_ids = [
        row[0]
        for row in (
            db.session.query(Order.id)
            .filter(
                Order.stock_reserved.is_(True),
                Order.status == "pending_payment",
                Order.mp_preference_expires_at.is_(None),
            )
            .all()
        )
    ]
    if not candidate_ids:
        return 0
    latest_by_order = _latest_payments_by_order(candidate_ids)
    return sum(
        1
        for order_id in candidate_ids
        if (
            latest_by_order.get(order_id) is None
            or latest_by_order.get(order_id).status != "approved"
        )
    )


def find_expired_stock_reservations(*, hours=None, limit=None, now=None):
    """Lista candidatos comprovadamente vencidos, sem mutação."""
    load_reservation_hours(hours)  # valida configuração; prazo persistido manda.
    limit = load_release_limit(limit)
    effective_now = now or utcnow()
    candidates = (
        Order.query
        .filter(
            Order.stock_reserved.is_(True),
            Order.status == "pending_payment",
            Order.mp_preference_expires_at.isnot(None),
            Order.mp_preference_expires_at < effective_now,
        )
        .order_by(Order.mp_preference_expires_at.asc(), Order.id.asc())
        .limit(limit)
        .all()
    )
    latest_by_order = _latest_payments_by_order([o.id for o in candidates])
    eligible = []
    for order in candidates:
        payment = latest_by_order.get(order.id)
        if payment is None or payment.status != "approved":
            eligible.append(order)
    return eligible


def release_reserved_stock(order):
    """Devolve ao estoque o que foi reservado em um pedido não pago.

    Caminho compartilhado de restituição (webhook refunded/payment_failed e
    rejeição administrativa de manual_review — item 1.3). Não altera status:
    quem chama decide o desfecho do pedido.

    Para itens com variante, resolve sempre Product primeiro e ProductVariant
    depois (ordem de lock consistente com o abatimento na criação). Se a
    variante não resolver (id inexistente ou pertencente a outro produto),
    ABORTA com erro para que a transação faça rollback — jamais restaura
    apenas o agregado (Product.stock) e deixa a soma das variantes
    divergente. A devolução é limitada ao estoque informado pelo
    fornecedor (``supplier_stock``): se ele reduziu o estoque, o pedido
    vencido não devolve acima do que o fornecedor ainda tem. Quando a
    variante está desabilitada comercialmente, a devolução restaura o
    estoque DELA mas não entra no agregado (soma das ATIVAS) — veja
    ``_apply_release_delta``.
    """
    if not order.stock_reserved:
        return

    own = _own_reserved_by_variant(order)
    for item in order.items:
        if item.variant_id is not None:
            # Guard: sem product_id não há como validar a integridade da
            # variante — aborta em vez de restaurar cegamente.
            if item.product_id is None:
                raise ValueError(
                    f"Item inválido em {item.product_name}: "
                    "variant_id sem product_id não pode ser restituído."
                )

            product = (
                Product.query
                .filter_by(id=item.product_id)
                .with_for_update()
                .populate_existing()
                .first()
            )
            if product is None:
                raise ValueError(
                    f"Produto não encontrado para restituir: {item.product_name}."
                )

            variant = (
                ProductVariant.query
                .filter_by(id=item.variant_id, product_id=item.product_id)
                .with_for_update()
                .populate_existing()
                .first()
            )
            if variant is None:
                # Erro de integridade: não restaurar Product.stock isoladamente.
                raise ValueError(
                    f"Variação inválida ao restituir: {item.product_name}."
                )

            others = 0
            if variant.supplier_stock is not None:
                others = _others_reserved_for(
                    variant, own.get(variant.id, 0)
                )
            delta = _restock_variant(
                variant, item.quantity, others_reserved=others
            )
            _apply_release_delta(product, variant, delta)
        else:
            if item.product_id is None:
                continue

            product = (
                Product.query
                .filter_by(id=item.product_id)
                .with_for_update()
                .first()
            )
            if product is not None and product.stock is not None:
                product.stock += item.quantity

    # Só após TODOS os itens restaurados com sucesso a reserva é encerrada.
    order.stock_reserved = False


def _release_stock_locked(order):
    """Restitui a reserva vencida e transiciona o pedido para "expired".

    Requer o Order já travado. Só é chamado pelos caminhos de liberação por
    expiração, que garantem pending_payment; o pagamento aprovado tardio
    é reconstruído em _sync_payment (re-debita o estoque e vira "paid").
    A restituição à variante desabilitada restaura o estoque DELA sem
    entrar no agregado (``_apply_release_delta``); o encerramento
    (`stock_reserved = False` + status) é idempotente via guard.
    """
    if not order.stock_reserved or order.status != "pending_payment":
        return False

    own = _own_reserved_by_variant(order)
    for item in order.items:
        if item.variant_id is not None:
            if item.product_id is None:
                raise ValueError("stock_release_item_integrity_invalid")

            product = (
                Product.query
                .filter_by(id=item.product_id)
                .with_for_update()
                .populate_existing()
                .first()
            )
            if product is None:
                raise ValueError("stock_release_product_missing")

            variant = (
                ProductVariant.query
                .filter_by(id=item.variant_id, product_id=item.product_id)
                .with_for_update()
                .populate_existing()
                .first()
            )
            if variant is None:
                raise ValueError("stock_release_variant_invalid")

            others = 0
            if variant.supplier_stock is not None:
                others = _others_reserved_for(
                    variant, own.get(variant.id, 0)
                )
            delta = _restock_variant(
                variant, item.quantity, others_reserved=others
            )
            _apply_release_delta(product, variant, delta)
        else:
            if item.product_id is None:
                continue
            product = (
                Product.query
                .filter_by(id=item.product_id)
                .with_for_update()
                .first()
            )
            if product is not None and product.stock is not None:
                product.stock += item.quantity

    order.stock_reserved = False
    order.status = "expired"
    return True


def release_single_expired_stock_reservation(
    order_id, *, source="admin", now=None
):
    """Libera um pedido sob lock, aplicando todas as proteções canônicas."""
    effective_now = now or utcnow()
    try:
        order = (
            Order.query
            .filter_by(id=order_id)
            .with_for_update(of=Order)
            .first()
        )
        if not _release_eligible(order, now=effective_now):
            db.session.rollback()
            return False
        released = _release_stock_locked(order)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    current_app.logger.info(
        "stock_reservation_single_release source=%s released=%s",
        str(source or "admin")[:32],
        int(bool(released)),
    )
    return released


def release_expired_stock_reservations(
    *,
    hours=None,
    limit=None,
    source="service",
    now=None,
):
    """Libera reservas vencidas com lock e revalidação fail-closed."""
    hours = load_reservation_hours(hours)
    limit = load_release_limit(limit)
    effective_now = now or utcnow()
    source = str(source or "service")[:32]

    candidate_ids = [
        row[0]
        for row in (
            db.session.query(Order.id)
            .filter(
                Order.stock_reserved.is_(True),
                Order.status == "pending_payment",
                Order.mp_preference_expires_at.isnot(None),
                Order.mp_preference_expires_at < effective_now,
            )
            .order_by(Order.mp_preference_expires_at.asc(), Order.id.asc())
            .limit(limit)
            .all()
        )
    ]

    released = 0
    skipped = 0
    # Sem prefetch aqui de propósito: o pagamento é relido sob o lock da
    # linha (serializa com o webhook), em vez de uma foto anterior ao lock.
    try:
        for order_id in candidate_ids:
            order = (
                Order.query
                .filter_by(id=order_id)
                .with_for_update(of=Order, skip_locked=True)
                .first()
            )
            if not _release_eligible(order, now=effective_now):
                skipped += 1
                continue
            if _release_stock_locked(order):
                released += 1
            else:
                skipped += 1

        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    current_app.logger.info(
        "stock_reservation_cleanup source=%s candidates=%s released=%s skipped=%s",
        source,
        len(candidate_ids),
        released,
        skipped,
    )
    return StockReservationReleaseResult(
        candidates=len(candidate_ids),
        released=released,
        skipped=skipped,
        hours=hours,
        source=source,
    )
