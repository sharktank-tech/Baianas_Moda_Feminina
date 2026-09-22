"""Prontidão para primeira venda real (somente leitura, fail-closed).

Verificação administrativa para identificar produtos que podem receber
divulgação sem risco conhecido de fornecedor, frete, preço ou margem.
Nunca habilita fulfillment, nunca compra no fornecedor, nunca altera
preços e nunca cria dados fictícios.

Mapa de campos canônicos utilizados (conferidos em modelos/migrations):
- publicação/preço: Product.is_active, is_draft, pricing_status
  ('priced' exigido), price, target_margin_percent ((0,100)),
  name/description, image1.
- custo/margem: SupplierProduct.cost, Product.cost (estoque próprio),
  SupplierProduct.currency/freight_currency, Product.currency (BRL
  exigido), SupplierProduct.shipping_cost/landed_cost/freight_status;
  economia calculada SOMENTE via
  ``evaluate_unit_economics`` + ``build_unit_economics_components`` +
  ``load_unit_economics_cost_policy`` (nenhuma fórmula duplicada).
- fornecedor/SKU/variante: SupplierProduct.is_active,
  supplier_product_id (SKU canônico), ProductVariant.is_active/stock/
  supplier_product_id; Supplier NÃO possui is_active — aplicabilidade
  do fornecedor vem do vínculo ativo + existência da linha.
- estoque: Product.stock / soma das variantes vendáveis.
- frete/cotação: ``SHIPPING_ALIEXPRESS_FRESH_QUOTES`` (prova fresca por
  vínculo: cost, currency, quoted_at, ttl_seconds), QUOTE_TTL_SECONDS
  como TTL padrão; ``SHIPPING_TEMPORARY_FLAT_RATE`` (R$ 10) é
  receita/cobrança temporária e NUNCA prova de custo logístico.
- origem: mesma semântica de ``classify_line`` (aliexpress / domestic /
  unknown); estoque próprio exige origem 'own' comprovada via
  ``resolve_group_origin``.

Estados: PRONTO (tudo comprovado), REVISÃO (sem falha definitiva, falta
evidência operacional), BLOQUEADO (inconsistência, dado inválido ou risco
financeiro). Ausência de informação nunca gera PRONTO.

Múltiplos vínculos: avaliação por variante NÃO é implementada nesta
versão. Produtos (com ou sem variantes) que apresentem mais de um vínculo
ativo permanecem BLOQUEADOS (ambiguous_supplier); cada variante vendável
precisa resolver para o vínculo único aplicável do produto.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Optional

READY = "PRONTO"
REVIEW = "REVISÃO"
BLOCKED = "BLOQUEADO"

BLOCKING = "blocking"
REVIEW_ONLY = "review"
INFO = "info"

# Severidade por código. missing_image e shipping_cost_unproven são falta
# de evidência operacional (REVISÃO); manual_fulfillment_required é
# informativo e nunca altera o estado.
REASON_SEVERITY = {
    "product_inactive": BLOCKING,
    "product_draft": BLOCKING,
    "pricing_not_ready": BLOCKING,
    "missing_sale_price": BLOCKING,
    "missing_description": BLOCKING,
    "missing_image": REVIEW_ONLY,
    "no_stock": BLOCKING,
    "missing_supplier": BLOCKING,
    "ambiguous_supplier": BLOCKING,
    "missing_sku": BLOCKING,
    "missing_origin": BLOCKING,
    "missing_cost": BLOCKING,
    "currency_mismatch": BLOCKING,
    "landed_cost_stale": BLOCKING,
    "shipping_cost_unproven": REVIEW_ONLY,
    "shipping_quote_expired": BLOCKING,
    "economics_incomplete": BLOCKING,
    "negative_margin": BLOCKING,
    "manual_fulfillment_required": INFO,
}

REASON_LABELS = {
    "product_inactive": "Produto inativo",
    "product_draft": "Produto em rascunho",
    "pricing_not_ready": "Precificação não concluída (exige status priced)",
    "missing_sale_price": "Preço de venda ausente ou inválido",
    "missing_description": "Nome ou descrição ausentes",
    "missing_image": "Imagem principal ausente",
    "no_stock": "Sem estoque vendável",
    "missing_supplier": "Sem vínculo de fornecedor aplicável",
    "ambiguous_supplier": "Fornecedor ambíguo ou inválido",
    "missing_sku": "SKU canônico ausente",
    "missing_origin": "Origem de envio não comprovada",
    "missing_cost": "Custo do item não comprovado",
    "currency_mismatch": "Moeda diferente de BRL",
    "landed_cost_stale": "Custo total inconsistente (custo + frete)",
    "shipping_cost_unproven": "Custo logístico sem prova fresca",
    "shipping_quote_expired": "Cotação de frete vencida",
    "economics_incomplete": "Economia unitária incompleta",
    "negative_margin": "Margem negativa (prejuízo)",
    "manual_fulfillment_required": "Tratamento manual obrigatório após pagamento",
}

READY_LIMIT = 500
CANDIDATE_LIMIT = 5


def _brl(value) -> str:
    if value is None:
        return "—"
    return "R$ %s" % format(Decimal(value), ".2f").replace(".", ",")


def _percent(value) -> str:
    if value is None:
        return "—"
    return "%s%%" % format(Decimal(value), ".2f").replace(".", ",")


@dataclass(frozen=True)
class ProductReadiness:
    product_id: object
    name: str
    origin: str
    supplier_name: str
    price_display: str
    proven_cost_display: str
    charged_freight_display: str
    proven_logistic_display: str
    margin_display: str
    stock: int
    state: str
    reasons: tuple = ()
    margin_sort: Optional[Decimal] = None


@dataclass(frozen=True)
class SalesReadinessSummary:
    rows: tuple = ()
    pronto: int = 0
    revisao: int = 0
    bloqueado: int = 0
    evaluated: int = 0
    candidates: tuple = ()


def summarize_readiness(rows) -> SalesReadinessSummary:
    """Agregação pura (testável com lista vazia)."""
    rows = tuple(rows or ())
    pronto = [r for r in rows if r.state == READY]
    revisao = [r for r in rows if r.state == REVIEW]
    bloqueado = [r for r in rows if r.state == BLOCKED]
    candidates = tuple(
        sorted(
            pronto,
            key=lambda r: (
                -(r.margin_sort if r.margin_sort is not None else Decimal("-1")),
                -r.stock,
                str(r.product_id),
            ),
        )[:CANDIDATE_LIMIT]
    )
    return SalesReadinessSummary(
        rows=rows,
        pronto=len(pronto),
        revisao=len(revisao),
        bloqueado=len(bloqueado),
        evaluated=len(rows),
        candidates=candidates,
    )


def _fresh_logistic_cost(link_id, fresh_quotes, now):
    """Custo logístico comprovado por cotação fresca do vínculo.

    Retorna (valor|None, motivo|None). Ausência de prova é REVISÃO;
    prova vencida ou em moeda incompatível é BLOQUEADO. Nunca estima.
    """
    from loja.services.shipping_quote import QUOTE_TTL_SECONDS

    entry = (fresh_quotes or {}).get(str(link_id))
    if not isinstance(entry, dict):
        return None, "shipping_cost_unproven"
    if str(entry.get("currency") or "BRL").strip().upper() != "BRL":
        return None, "currency_mismatch"
    from loja.services.pricing import to_decimal

    try:
        cost = to_decimal(entry.get("cost"))
    except Exception:
        return None, "shipping_cost_unproven"
    if cost is None or not cost.is_finite() or cost < 0:
        return None, "shipping_cost_unproven"
    try:
        quoted_at = entry.get("quoted_at")
        ttl = int(entry.get("ttl_seconds") or QUOTE_TTL_SECONDS)
        if quoted_at is None or now - quoted_at > timedelta(seconds=ttl):
            return None, "shipping_quote_expired"
    except (TypeError, ValueError):
        return None, "shipping_cost_unproven"
    return cost.quantize(Decimal("0.01")), None


def evaluate_product_readiness(
    product, *, policy=None, fresh_quotes=None, now=None, charged_freight=None
):
    """Avalia um produto. Requer contexto da aplicação (config/origens)."""
    from datetime import timezone

    from loja.services.pricing import normalize_currency, to_decimal
    from loja.services.shipping_quote import (
        resolve_group_origin,
        temporary_flat_rate,
    )
    from loja.services.shipping_quote import QuoteUnavailable
    from loja.services.unit_economics import evaluate_unit_economics
    from loja.services.unit_economics_policy import (
        build_unit_economics_components,
        load_unit_economics_cost_policy,
    )
    from loja.utils import utcnow

    effective_now = now or utcnow()
    if effective_now.tzinfo is not None:
        effective_now = effective_now.astimezone(timezone.utc).replace(tzinfo=None)

    blocking = []
    review = []

    def _add(code):
        (blocking if REASON_SEVERITY[code] == BLOCKING else review).append(code)

    # ── Produto ──
    if not getattr(product, "is_active", False):
        _add("product_inactive")
    if getattr(product, "is_draft", False):
        _add("product_draft")
    if getattr(product, "pricing_status", None) != "priced":
        _add("pricing_not_ready")
    price = to_decimal(getattr(product, "price", None))
    if price is None or not price.is_finite() or price <= 0:
        _add("missing_sale_price")
        price = None
    if not (getattr(product, "name", "") or "").strip() or not (
        getattr(product, "description", "") or ""
    ).strip():
        _add("missing_description")
    if not (getattr(product, "image1", "") or "").strip():
        _add("missing_image")

    # ── Fornecedor/origem ──
    links = [
        link
        for link in (getattr(product, "supplier_links", None) or [])
        if getattr(link, "is_active", False)
    ]
    source = getattr(product, "source", None) or "internal"
    supplier = None
    link = None
    origin = "Desconhecida"
    supplier_name = "—"
    if source == "aliexpress":
        origin = "AliExpress"
        if len(links) == 0:
            _add("missing_supplier")
        elif len(links) > 1:
            _add("ambiguous_supplier")
        else:
            link = links[0]
            supplier = getattr(link, "supplier", None)
            if (
                supplier is None
                or getattr(supplier, "channel_type", None) != "aliexpress"
                or not getattr(supplier, "seller_id", None)
            ):
                _add("ambiguous_supplier")
                supplier = None
                link = None
            elif not getattr(link, "supplier_product_id", None):
                _add("missing_sku")
            else:
                supplier_name = getattr(supplier, "name", None) or "—"
    else:
        distinct_suppliers = {
            getattr(link, "supplier_id", None) for link in links
        } - {None}
        if len(links) == 0:
            origin = "Estoque próprio"
            supplier_name = "Estoque próprio"
            proven_origin = resolve_group_origin("domestic", None)
            if not proven_origin:
                _add("missing_origin")
        elif len(links) > 1 or len(distinct_suppliers) > 1:
            origin = "Nacional"
            _add("ambiguous_supplier")
        else:
            link = links[0]
            supplier = getattr(link, "supplier", None)
            if supplier is None:
                _add("missing_supplier")
                link = None
            else:
                origin = "Nacional"
                supplier_name = getattr(supplier, "name", None) or "—"

    # ── Variantes ──
    variants = list(getattr(product, "variants", None) or [])

    def _variant_applicable(variant):
        if not getattr(variant, "is_active", False):
            return False
        try:
            if int(getattr(variant, "stock", 0) or 0) <= 0:
                return False
        except (TypeError, ValueError):
            return False
        bound = getattr(variant, "supplier_product_id", None)
        if bound is None:
            if link is not None or (
                source != "aliexpress" and len(links) == 0
            ):
                return True
            return False
        if link is not None and str(bound) == str(link.id):
            return True
        return False

    def _variant_flaw(variant):
        try:
            nostock = int(getattr(variant, "stock", 0) or 0) <= 0
        except (TypeError, ValueError):
            nostock = True
        if nostock:
            return "no_stock"
        bound = getattr(variant, "supplier_product_id", None)
        if bound is None:
            if link is None and not (
                source != "aliexpress" and len(links) == 0
            ):
                return "ambiguous_supplier"
            return None
        candidate = next(
            (
                cand
                for cand in links
                if str(getattr(cand, "id", None)) == str(bound)
            ),
            None,
        )
        if candidate is None:
            return "missing_supplier"
        return None

    if variants:
        sellable = [v for v in variants if _variant_applicable(v)]
        if not sellable:
            _add("no_stock")
            stock = 0
        else:
            stock = sum(int(v.stock or 0) for v in sellable)
            for variant in variants:
                if getattr(variant, "is_active", False) and variant not in sellable:
                    flaw = _variant_flaw(variant)
                    if flaw == "no_stock":
                        review.append(flaw)  # visível, sem bloquear os vendáveis
                    elif flaw is not None:
                        _add(flaw)
    else:
        try:
            stock = int(getattr(product, "stock", 0) or 0)
        except (TypeError, ValueError):
            stock = 0
        if stock <= 0:
            _add("no_stock")

    # ── Custo/economia (serviço canônico, sem fórmulas duplicadas) ──
    proven_cost = None
    proven_logistic = None
    margin_percent = None
    if link is not None:
        cost = to_decimal(getattr(link, "cost", None))
        if cost is None or not cost.is_finite() or cost <= 0:
            _add("missing_cost")
        elif (
            normalize_currency(getattr(product, "currency", None)) != "BRL"
            or normalize_currency(getattr(link, "currency", None)) != "BRL"
            or normalize_currency(getattr(link, "freight_currency", None)) != "BRL"
        ):
            _add("currency_mismatch")
        else:
            shipping = to_decimal(getattr(link, "shipping_cost", None))
            landed = to_decimal(getattr(link, "landed_cost", None))
            persisted_ok = (
                landed is not None
                and landed.is_finite()
                and landed > 0
                and shipping is not None
                and shipping.is_finite()
                and shipping >= 0
                and getattr(link, "freight_status", None) == "available"
                and landed == (cost + shipping).quantize(Decimal("0.01"))
            )
            # Prova logística fresca, independente do snapshot persistido.
            # A margem SEMPRE usa current_landed (custo do item + frete
            # fresco); o persistido serve só para detectar divergência.
            fresh_cost, fresh_reason = _fresh_logistic_cost(
                link.id, fresh_quotes, effective_now
            )
            if fresh_cost is not None:
                proven_logistic = fresh_cost
                current_landed = (cost + fresh_cost).quantize(Decimal("0.01"))
                if persisted_ok and abs(landed - current_landed) > Decimal("0.01"):
                    _add("landed_cost_stale")
                proven_cost = current_landed
            elif fresh_reason == "currency_mismatch":
                _add("currency_mismatch")
            elif fresh_reason == "shipping_quote_expired":
                _add("shipping_quote_expired")
            elif persisted_ok:
                proven_cost = landed
                # Item comprovado, falta só a prova logística → REVISÃO.
                review.append("shipping_cost_unproven")
            else:
                _add("missing_cost")
    elif source != "aliexpress" and len(links) == 0:
        item_cost = to_decimal(getattr(product, "cost", None))
        if item_cost is None or not item_cost.is_finite() or item_cost <= 0:
            _add("missing_cost")
        elif normalize_currency(getattr(product, "currency", None)) != "BRL":
            _add("currency_mismatch")
        else:
            proven_cost = item_cost.quantize(Decimal("0.01"))
            # Estoque próprio não tem prova logística por produto: teto REVISÃO.
            review.append("shipping_cost_unproven")

    if proven_cost is not None and price is not None:
        target = to_decimal(getattr(product, "target_margin_percent", None))
        if (
            target is None
            or not target.is_finite()
            or target <= 0
            or target >= 100
        ):
            _add("economics_incomplete")
        else:
            try:
                active_policy = (
                    policy if policy is not None
                    else load_unit_economics_cost_policy()
                )
                variable, operational = build_unit_economics_components(
                    charged_amount=price,
                    discount_amount=Decimal("0.00"),
                    policy=active_policy,
                )
                from loja.services.unit_economics import UnitEconomicsError

                evaluation = evaluate_unit_economics(
                    sale_price=price,
                    landed_cost=proven_cost,
                    variable_costs=variable,
                    operational_costs=operational,
                )
            except Exception:
                evaluation = None
            if (
                evaluation is None
                or not evaluation.complete
                or evaluation.operational_margin is None
                or evaluation.operational_margin_percent is None
            ):
                _add("economics_incomplete")
            elif evaluation.operational_margin < 0:
                _add("negative_margin")
            else:
                margin_percent = evaluation.operational_margin_percent
    elif price is not None and "missing_cost" not in blocking:
        # Preço existe mas custo não foi comprovado por outro motivo.
        if "currency_mismatch" not in blocking:
            _add("economics_incomplete")

    # ── Frete cobrado (receita temporária, nunca prova de custo) ──
    if charged_freight is None:
        try:
            charged_freight = temporary_flat_rate()
        except QuoteUnavailable:
            charged_freight = None

    state = BLOCKED if blocking else (REVIEW if review else READY)
    reasons = tuple(blocking + review + ["manual_fulfillment_required"])
    return ProductReadiness(
        product_id=getattr(product, "id", None),
        name=getattr(product, "name", None) or "—",
        origin=origin,
        supplier_name=supplier_name,
        price_display=_brl(price),
        proven_cost_display=_brl(proven_cost),
        charged_freight_display=_brl(charged_freight),
        proven_logistic_display=_brl(proven_logistic),
        margin_display=_percent(margin_percent),
        stock=int(stock or 0),
        state=state,
        reasons=reasons,
        margin_sort=margin_percent,
    )


def load_fresh_quotes_snapshot():
    """Fotografia única e validada das cotações frescas (sem provider).

    Fonte canônica: ``SHIPPING_ALIEXPRESS_FRESH_QUOTES``. Ausente ou
    inválida (não-dict) vira ``{}`` fail-closed. O entrypoint lê uma vez
    e repassa a mesma fotografia a todos os produtos do GET.
    """
    from flask import current_app

    raw = current_app.config.get("SHIPPING_ALIEXPRESS_FRESH_QUOTES")
    if not isinstance(raw, dict):
        return {}
    return raw


def evaluate_sales_readiness(*, limit=READY_LIMIT) -> SalesReadinessSummary:
    """Avalia o catálogo (somente leitura). Requer contexto da aplicação."""
    from loja.modules.models import Product

    fresh_snapshot = load_fresh_quotes_snapshot()
    products = (
        Product.query.order_by(Product.name.asc(), Product.id.asc())
        .limit(limit)
        .all()
    )
    return summarize_readiness(
        evaluate_product_readiness(product, fresh_quotes=fresh_snapshot)
        for product in products
    )


__all__ = [
    "READY",
    "REVIEW",
    "BLOCKED",
    "REASON_LABELS",
    "REASON_SEVERITY",
    "READY_LIMIT",
    "CANDIDATE_LIMIT",
    "ProductReadiness",
    "SalesReadinessSummary",
    "summarize_readiness",
    "load_fresh_quotes_snapshot",
    "evaluate_product_readiness",
    "evaluate_sales_readiness",
]
