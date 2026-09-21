"""Cotação de frete por grupo de remessa/fornecedor (revisão comercial).

Premissas (não presumir origem universal):
- Envio nacional usa a origem efetiva de postagem + destino do cliente.
  Sem origem configurada (``SHIPPING_ORIGIN_CEP``) ou sem fonte de tarifa,
  NÃO há cotação — o checkout bloqueia com mensagem e permite retry.
- Envio internacional/AliExpress usa custo fresco e comprovado por vínculo
  (destino, opção, quantidade, moeda e momento verificados). Custo
  persistido genérico sem essa comprovação bloqueia (``unverified_cost``).
  Nunca passa envio internacional pelo simulador doméstico, nem cobra
  tarifa inventada por distância.
- Fornecedor não identificado nunca vira grupo nacional: vira grupo
  "unknown" e bloqueia (``unknown_supplier``).
- R$ 10 é referência inicial informada pelo proprietário, jamais cotação
  nacional garantida: só aparece rotulada como estimativa e nunca entra
  em totais, pedidos ou preferência Mercado Pago. A distinção entre
  cotação real e estimativa é estrutural (objetos ``Quote`` de
  provedores), nunca pelo valor numérico: R$ 10,00 cotado de verdade
  é cobrado normalmente.

Sem chamadas reais a transportadoras: provedores recebem dados explícitos
(origem, destino, tabela) e falham fechado quando falta insumo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from loja.utils import utcnow

from .pricing import to_decimal

#: Referência inicial informada pelo proprietário. Uso exclusivo em rótulo
#: de estimativa pré-CEP; jamais como valor cobrado.
SHIPPING_ESTIMATE_REFERENCE = Decimal("10.00")

#: Validade padrão de uma cotação exibida (segundos).
QUOTE_TTL_SECONDS = 1800

_CEP_RE = re.compile(r"^\d{8}$")


def normalize_cep(value) -> Optional[str]:
    """Normaliza CEP para 8 dígitos ou retorna None."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if _CEP_RE.match(digits) else None


def estimate_label() -> str:
    """Rótulo pré-CEP quando não há base real para valor inicial.

    Não sugere tarifa: apenas informa que o frete sai após o CEP.
    Nunca usar como valor cobrado.
    """
    return "Frete calculado após informar o CEP"


class QuoteUnavailable(Exception):
    """Cotação impossível no momento. Códigos estáveis (sem PII):

    missing_origin        -> sem origem de postagem configurada
    provider_unavailable  -> fornecedor sem fonte de tarifa/custo
    unverified_cost       -> custo persistido sem comprovação de que vale
                             para este CEP, opção, quantidade e momento
    unknown_supplier      -> fornecedor não identificado (nunca é nacional)
    no_supplier_data      -> item sem vínculo de fornecedor cotável
    currency_mismatch     -> moeda do frete incompatível (sem FX)
    invalid_destination   -> CEP de destino inválido
    expired               -> cotação vencida
    invalid_quote         -> cotação incomparável
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ShipmentLine:
    product_id: object
    variant_id: object = None
    quantity: int = 1


@dataclass(frozen=True)
class ShipmentGroup:
    """Um grupo = uma remessa. Chave: supplier_id (AliExpress) ou None.

    channel: "domestic" (nacional, origem conhecida), "aliexpress"
    (fornecedor identificado) ou "unknown" (fornecedor não identificado —
    bloqueia, nunca vira nacional). option: opção de envio (None = padrão).
    """

    key: object
    channel: str
    lines: tuple = field(default_factory=tuple)
    option: object = None


@dataclass(frozen=True)
class Quote:
    cost: Decimal
    min_days: Optional[int] = None
    max_days: Optional[int] = None
    carrier: str = ""
    quoted_at: object = None
    ttl_seconds: int = QUOTE_TTL_SECONDS

    def expires_at(self):
        from datetime import timedelta

        base = self.quoted_at or utcnow()
        return base + timedelta(seconds=self.ttl_seconds)

    def is_expired(self, now=None) -> bool:
        return self.expires_at() <= (now or utcnow())


def validate_quote(quote, now=None) -> Quote:
    """Rejeita cotação ausente, incomparável ou vencida."""
    if not isinstance(quote, Quote):
        raise QuoteUnavailable("invalid_quote")
    try:
        cost = to_decimal(quote.cost)
    except Exception:
        raise QuoteUnavailable("invalid_quote") from None
    if cost is None or cost < 0:
        raise QuoteUnavailable("invalid_quote")
    if quote.is_expired(now):
        raise QuoteUnavailable("expired")
    return quote


def assert_not_estimate(value) -> None:
    """Impede que texto de exibição vire valor cobrado.

    A distinção entre cotação real e estimativa é estrutural: só totais
    derivados de objetos ``Quote`` de provedores chegam à cobrança, e o
    rótulo de estimativa nunca é parseado. Valores numéricos — inclusive
    R$ 10,00 cotado de verdade pelo fornecedor — são legítimos; texto de
    vitrine (não parseável como dinheiro) é rejeitado aqui.
    """
    if isinstance(value, Quote):
        return
    if isinstance(value, str) and to_decimal(value) is None:
        raise QuoteUnavailable("invalid_quote")


class TableDomesticProvider:
    """Simulador doméstico explícito (testes e operação assistida).

    Recebe origem + tabela de tarifas por prefixo de CEP; sem origem ou sem
    entrada para o destino, falha fechado. NÃO inventa tarifa por distância:
    só responde o que está na tabela. Em produção, sem configuração, o
    checkout bloqueia (mensagem + retry) em vez de cobrar valor presumido.
    """

    def __init__(self, origin_cep, rates: dict):
        self.origin_cep = normalize_cep(origin_cep)
        self.rates = rates or {}

    def quote(self, group: ShipmentGroup, dest_cep: str, now=None) -> Quote:  # noqa: ARG002
        dest = normalize_cep(dest_cep)
        if dest is None:
            raise QuoteUnavailable("invalid_destination")
        if self.origin_cep is None:
            raise QuoteUnavailable("missing_origin")
        entry = self.rates.get(dest[:3]) or self.rates.get(dest[:2])
        if entry is None:
            raise QuoteUnavailable("provider_unavailable")
        cost, min_days, max_days = entry
        return Quote(
            cost=to_decimal(cost),
            min_days=min_days,
            max_days=max_days,
            carrier="simulated-domestic",
            quoted_at=utcnow(),
        )


class AliExpressSupplierProvider:
    """Cotação AliExpress somente com custo fresco e comprovado.

    O ``SupplierProduct.shipping_cost`` persistido é genérico/obsoleto por
    natureza: não comprova destino, opção de envio, quantidade nem momento.
    Por isso a cotação exige um contexto fresco por vínculo
    (``fresh_quotes[str(supplier_product_id)]`` com custo, prefixo de
    destino, opção, quantidade, moeda, momento e validade) — em produção,
    alimentado pela integração de frete do fornecedor; sem ele, bloqueia
    com ``unverified_cost`` em vez de assumir que o custo anterior vale.
    Prazo vem do contexto fresco (pode ser None = "prazo do fornecedor").
    Frete sem comprovação nunca cai no simulador doméstico.
    """

    def __init__(self, fresh_quotes=None):
        self.fresh_quotes = fresh_quotes or {}

    def quote(self, group: ShipmentGroup, dest_cep: str, now=None) -> Quote:
        from loja.modules.models import ProductVariant, SupplierProduct

        dest = normalize_cep(dest_cep)
        if dest is None:
            raise QuoteUnavailable("invalid_destination")
        current = now or utcnow()
        # Agrega quantidade por vínculo (o custo fresco vale para qty exata).
        per_sp: dict = {}
        order: list = []
        for line in group.lines:
            sp = None
            if line.variant_id is not None:
                variant = ProductVariant.query.filter_by(id=line.variant_id).first()
                if variant is not None and variant.supplier_product_id:
                    sp = SupplierProduct.query.filter_by(
                        id=variant.supplier_product_id
                    ).first()
            if sp is None and line.product_id is not None:
                candidates = SupplierProduct.query.filter_by(
                    product_id=line.product_id, is_active=True
                ).all()
                actives = [c for c in candidates if c.is_active]
                if len(actives) == 1:
                    sp = actives[0]
            if sp is None:
                raise QuoteUnavailable("no_supplier_data")
            key = str(sp.id)
            if key not in per_sp:
                per_sp[key] = {"qty": 0}
                order.append(key)
            per_sp[key]["qty"] += int(line.quantity or 1)
        total = Decimal("0.00")
        min_days = max_days = None
        for key in order:
            fresh = self.fresh_quotes.get(key)
            problem = self._check_fresh(
                fresh, dest, group.option, per_sp[key]["qty"], current
            )
            if problem is not None:
                raise QuoteUnavailable(problem)
            total += to_decimal(fresh["cost"])
            if fresh.get("min_days") is not None:
                min_days = fresh["min_days"] if min_days is None else min(min_days, fresh["min_days"])
            if fresh.get("max_days") is not None:
                max_days = fresh["max_days"] if max_days is None else max(max_days, fresh["max_days"])
        return Quote(
            cost=total,
            min_days=min_days,
            max_days=max_days,
            carrier="supplier",
            quoted_at=current,
        )

    @staticmethod
    def _check_fresh(fresh, dest: str, option, qty: int, now) -> Optional[str]:
        """Prova estrita: retorna None se o custo vale para este cenário,
        ou o código do bloqueio (destino, opção, quantidade, moeda, valor,
        validade)."""
        if not isinstance(fresh, dict):
            return "unverified_cost"
        prefix = str(fresh.get("dest_prefix") or "")
        if not prefix or not dest.startswith(prefix):
            return "unverified_cost"
        if (fresh.get("option") or None) != (option or None):
            return "unverified_cost"
        try:
            if int(fresh.get("qty")) != int(qty):
                return "unverified_cost"
        except (TypeError, ValueError):
            return "unverified_cost"
        if str(fresh.get("currency") or "BRL").upper() != "BRL":
            return "currency_mismatch"
        try:
            cost = to_decimal(fresh.get("cost"))
        except Exception:
            return "unverified_cost"
        if cost is None or cost < 0:
            return "unverified_cost"
        try:
            quoted_at = fresh.get("quoted_at")
            ttl = int(fresh.get("ttl_seconds") or QUOTE_TTL_SECONDS)
            from datetime import timedelta

            if quoted_at is None or now - quoted_at > timedelta(seconds=ttl):
                return "expired"
        except (TypeError, ValueError):
            return "unverified_cost"
        return None


def _supplier_is_aliexpress(supplier) -> bool:
    return (
        supplier is not None
        and supplier.channel_type == "aliexpress"
        and bool(supplier.seller_id)
    )


def classify_line(product_id, variant_id=None):
    """Classifica um item em ("aliexpress", supplier_id), ("domestic", None)
    ou ("unknown", None). Requer contexto da aplicação.

    Produto nacional com origem conhecida (source != "aliexpress") vai para
    o grupo doméstico. Produto AliExpress sem fornecedor identificável
    (vínculo ausente, ambíguo ou inválido) é "unknown" e bloqueia o fluxo —
    nunca é classificado levianamente como envio nacional.
    """
    from loja.modules.models import Product, ProductVariant, Supplier, SupplierProduct

    supplier_id = None
    if variant_id is not None:
        variant = ProductVariant.query.filter_by(id=variant_id).first()
        if variant is not None and variant.supplier_product_id:
            sp = SupplierProduct.query.filter_by(
                id=variant.supplier_product_id
            ).first()
            if sp is not None and sp.supplier_id is not None:
                supplier = Supplier.query.filter_by(id=sp.supplier_id).first()
                if _supplier_is_aliexpress(supplier):
                    return "aliexpress", supplier.id
    product = (
        Product.query.filter_by(id=product_id).first()
        if product_id is not None
        else None
    )
    if product is not None:
        actives = SupplierProduct.query.filter_by(
            product_id=product.id, is_active=True
        ).all()
        valid = set()
        for sp in actives:
            if sp.supplier_id is None:
                continue
            supplier = Supplier.query.filter_by(id=sp.supplier_id).first()
            if _supplier_is_aliexpress(supplier):
                valid.add(supplier.id)
        if len(valid) == 1:
            return "aliexpress", next(iter(valid))
        if getattr(product, "source", None) != "aliexpress":
            return "domestic", None
    return "unknown", None


def rates_from_config(raw) -> dict:
    """Tabela doméstica a partir da config; formato inválido vira {} (bloqueio)."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        import json

        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        if isinstance(parsed, dict):
            # Normaliza valores [custo, min_dias, max_dias].
            normalized = {}
            for prefix, entry in parsed.items():
                try:
                    cost, min_days, max_days = entry
                    normalized[str(prefix)] = (
                        to_decimal(cost),
                        int(min_days),
                        int(max_days),
                    )
                except (TypeError, ValueError):
                    continue
            return normalized
    return {}


def default_providers():
    """Provedores da operação: origem/tabela doméstica via config (sem padrão).

    Sem ``SHIPPING_ORIGIN_CEP`` ou sem tabela, o grupo nacional falha
    fechado (missing_origin/provider_unavailable) em vez de presumir valor.
    """
    from flask import current_app

    origin = current_app.config.get("SHIPPING_ORIGIN_CEP")
    rates = rates_from_config(current_app.config.get("SHIPPING_DOMESTIC_RATES"))
    fresh = current_app.config.get("SHIPPING_ALIEXPRESS_FRESH_QUOTES") or {}
    if not isinstance(fresh, dict):
        fresh = {}
    return {
        "domestic": TableDomesticProvider(origin, rates),
        "aliexpress": AliExpressSupplierProvider(fresh_quotes=fresh),
    }


def quote_for_items(item_dicts, dest_cep: str, now=None) -> dict:
    """Cota itens de checkout (dicts com product_id/variant_id/quantity)."""
    lines = [
        ShipmentLine(
            product_id=item.get("product_id"),
            variant_id=item.get("variant_id"),
            quantity=int(item.get("quantity") or 1),
        )
        for item in item_dicts
    ]
    groups = group_lines(lines)
    return quote_groups(groups, dest_cep, default_providers(), now)


def canonical_items(item_dicts) -> list:
    """Forma canônica de itens para comparar cotação exibida × recálculo.

    Ordenada e só com identidade operacional (produto, variante,
    quantidade); nunca valores exibidos do navegador.
    """
    canonical = []
    for item in item_dicts:
        product_id = item.get("product_id")
        variant_id = item.get("variant_id")
        canonical.append(
            (
                str(product_id) if product_id is not None else "",
                str(variant_id) if variant_id is not None else "",
                int(item.get("quantity") or 1),
            )
        )
    return sorted(canonical)


def group_lines(lines, classify=None) -> list:
    """Agrupa linhas: nacional por origem conhecida, AliExpress por supplier.

    Fornecedor não identificado vira grupo "unknown" (bloqueia adiante),
    nunca nacional.
    """
    classify = classify or classify_line
    buckets: dict = {}
    for line in lines:
        channel, key = classify(line.product_id, line.variant_id)
        if channel == "aliexpress":
            bucket_key = ("aliexpress", key)
        elif channel == "unknown":
            bucket_key = ("unknown", None)
        else:
            bucket_key = ("domestic", None)
        bucket = buckets.setdefault(
            bucket_key, {"channel": channel, "key": key, "lines": []}
        )
        bucket["lines"].append(line)
    return [
        ShipmentGroup(
            key=info["key"], channel=info["channel"], lines=tuple(info["lines"])
        )
        for info in buckets.values()
    ]


def quote_groups(groups, dest_cep: str, providers: dict, now=None) -> dict:
    """Cota todos os grupos; soma custos e preserva prazos por grupo.

    Retorna {"total": Decimal, "groups": [{"key", "cost", "min_days",
    "max_days", "carrier", "quoted_at", "expires_at"}]}. Qualquer grupo sem
    cotação aborta tudo (nenhuma cobrança parcial).
    """
    dest = normalize_cep(dest_cep)
    if dest is None:
        raise QuoteUnavailable("invalid_destination")
    total = Decimal("0.00")
    breakdown = []
    for group in groups:
        if group.channel == "unknown":
            raise QuoteUnavailable("unknown_supplier")
        provider = providers.get(group.channel)
        if provider is None:
            raise QuoteUnavailable("provider_unavailable")
        quote = validate_quote(provider.quote(group, dest, now), now)
        total += to_decimal(quote.cost)
        breakdown.append(
            {
                "key": str(group.key) if group.key is not None else None,
                "channel": group.channel,
                "cost": to_decimal(quote.cost),
                "min_days": quote.min_days,
                "max_days": quote.max_days,
                "carrier": quote.carrier,
                "quoted_at": quote.quoted_at,
                "expires_at": quote.expires_at(),
            }
        )
    return {
        "total": total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        "groups": breakdown,
    }


__all__ = [
    "SHIPPING_ESTIMATE_REFERENCE",
    "QUOTE_TTL_SECONDS",
    "QuoteUnavailable",
    "ShipmentGroup",
    "ShipmentLine",
    "Quote",
    "normalize_cep",
    "canonical_items",
    "estimate_label",
    "validate_quote",
    "rates_from_config",
    "assert_not_estimate",
    "TableDomesticProvider",
    "AliExpressSupplierProvider",
    "classify_line",
    "default_providers",
    "quote_for_items",
    "group_lines",
    "quote_groups",
]
