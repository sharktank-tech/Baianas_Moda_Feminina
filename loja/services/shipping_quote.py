"""Cotação de frete por grupo de remessa/fornecedor (revisão comercial).

Premissas (não presumir origem universal):
- Envio nacional usa a origem efetiva de postagem + destino do cliente.
  Sem origem configurada (``SHIPPING_ORIGIN_CEP``) ou sem fonte de tarifa,
  NÃO há cotação — o checkout bloqueia com mensagem e permite retry.
- Envio internacional/AliExpress usa custo e opções informados pelo
  fornecedor (``SupplierProduct.shipping_cost`` com frete disponível e
  moeda coerente). Nunca passa envio internacional pelo simulador
  doméstico, nem cobra tarifa inventada por distância.
- R$ 10 é referência inicial informada pelo proprietário, jamais cotação
  nacional garantida: só aparece rotulada como estimativa e nunca entra
  em totais, pedidos ou preferência Mercado Pago.

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
    """Rótulo de estimativa pré-CEP. Nunca usar como valor cobrado."""
    return "a partir de R$ 10,00 (estimativa)"


class QuoteUnavailable(Exception):
    """Cotação impossível no momento. Códigos estáveis (sem PII):

    missing_origin        -> sem origem de postagem configurada
    provider_unavailable  -> fornecedor sem fonte de tarifa/custo
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
    """Um grupo = uma remessa. Chave: supplier_id (AliExpress) ou None (nacional)."""

    key: object
    channel: str  # "domestic" | "aliexpress"
    lines: tuple = field(default_factory=tuple)


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
    """Impede cobrar a referência de R$ 10 como fallback silencioso."""
    try:
        if to_decimal(value) == SHIPPING_ESTIMATE_REFERENCE and not isinstance(
            value, Quote
        ):
            raise QuoteUnavailable("invalid_quote")
    except QuoteUnavailable:
        raise
    except Exception:
        raise QuoteUnavailable("invalid_quote") from None


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

    def quote(self, group: ShipmentGroup, dest_cep: str) -> Quote:
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
    """Cotação AliExpress a partir dos dados persistidos do fornecedor.

    Usa ``SupplierProduct.shipping_cost`` somente com frete disponível e
    moedas coerentes em BRL (sem camada FX: moeda incompatível bloqueia).
    Prazo não é persistido no vínculo — retorna None (apresentar como
    "prazo do fornecedor no acompanhamento"). Frete indisponível nunca
    cai no simulador doméstico.
    """

    def quote(self, group: ShipmentGroup, dest_cep: str) -> Quote:  # noqa: ARG002
        from loja.modules.models import SupplierProduct

        if normalize_cep(dest_cep) is None:
            raise QuoteUnavailable("invalid_destination")
        costs = []
        for line in group.lines:
            sp = None
            if line.variant_id is not None:
                from loja.modules.models import ProductVariant

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
            if sp.freight_status != "available":
                raise QuoteUnavailable("provider_unavailable")
            shipping = to_decimal(sp.shipping_cost)
            if shipping is None or shipping < 0:
                raise QuoteUnavailable("provider_unavailable")
            currency = (sp.currency or "").strip().upper()
            freight_currency = (sp.freight_currency or "").strip().upper()
            if not currency or currency != freight_currency or currency != "BRL":
                raise QuoteUnavailable("currency_mismatch")
            costs.append(shipping)
        if not costs:
            raise QuoteUnavailable("no_supplier_data")
        return Quote(
            cost=sum(costs, Decimal("0.00")),
            min_days=None,
            max_days=None,
            carrier="supplier",
            quoted_at=utcnow(),
        )


def resolve_supplier_id(product_id, variant_id=None):
    """Resolve o fornecedor de um item para agrupamento de remessa.

    Retorna supplier_id quando o item pertence a um fornecedor AliExpress
    identificável (via variante ou vínculo ativo único do produto);
    caso contrário None (grupo nacional). Requer contexto da aplicação.
    """
    from loja.modules.models import ProductVariant, Supplier, SupplierProduct

    if variant_id is not None:
        variant = ProductVariant.query.filter_by(id=variant_id).first()
        if variant is not None and variant.supplier_product_id:
            sp = SupplierProduct.query.filter_by(
                id=variant.supplier_product_id
            ).first()
            if sp is not None and sp.supplier_id is not None:
                supplier = Supplier.query.filter_by(id=sp.supplier_id).first()
                if (
                    supplier is not None
                    and supplier.channel_type == "aliexpress"
                    and supplier.seller_id
                ):
                    return supplier.id
    if product_id is None:
        return None
    actives = SupplierProduct.query.filter_by(
        product_id=product_id, is_active=True
    ).all()
    valid = set()
    for sp in actives:
        if sp.supplier_id is None:
            continue
        supplier = Supplier.query.filter_by(id=sp.supplier_id).first()
        if (
            supplier is not None
            and supplier.channel_type == "aliexpress"
            and supplier.seller_id
        ):
            valid.add(supplier.id)
    if len(valid) == 1:
        return next(iter(valid))
    return None


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
    return {
        "domestic": TableDomesticProvider(origin, rates),
        "aliexpress": AliExpressSupplierProvider(),
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
    groups = group_lines(
        lines,
        lambda line: resolve_supplier_id(line.product_id, line.variant_id),
    )
    return quote_groups(groups, dest_cep, default_providers(), now)


def group_lines(lines, resolve_supplier) -> list:
    """Agrupa linhas por fornecedor: AliExpress por supplier_id, resto nacional."""
    buckets: dict = {}
    for line in lines:
        supplier_id = resolve_supplier(line)
        if supplier_id is None:
            key, channel = None, "domestic"
        else:
            key, channel = supplier_id, "aliexpress"
        bucket = buckets.setdefault(key, {"channel": channel, "lines": []})
        bucket["lines"].append(line)
    return [
        ShipmentGroup(key=key, channel=info["channel"], lines=tuple(info["lines"]))
        for key, info in buckets.items()
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
        provider = providers.get(group.channel)
        if provider is None:
            raise QuoteUnavailable("provider_unavailable")
        quote = validate_quote(provider.quote(group, dest), now)
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
    "estimate_label",
    "validate_quote",
    "rates_from_config",
    "assert_not_estimate",
    "TableDomesticProvider",
    "AliExpressSupplierProvider",
    "resolve_supplier_id",
    "default_providers",
    "quote_for_items",
    "group_lines",
    "quote_groups",
]
