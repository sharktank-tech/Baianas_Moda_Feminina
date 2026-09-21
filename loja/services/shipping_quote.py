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
- Com ``SHIPPING_GROUPED_QUOTE_ENABLED=False`` (padrão atual) a loja cobra
  a tarifa comercial temporária ``SHIPPING_TEMPORARY_FLAT_RATE`` via
  ``temporary_flat_rate()`` — regra assumida pela loja, validada como
  decimal finito ≥ 0 e arredondada em duas casas; não é cotação por CEP,
  sem frete grátis, e pode divergir do custo real do fornecedor.
- Com flag ligada, só cobra o que vem de ``Quote`` real de provedores;
  pré-CEP mostra apenas rótulo ("Frete calculado após informar o CEP"),
  nunca valor. A distinção entre tarifa temporária e cotação real é pela
  flag, não pelo valor numérico.

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

#: Validade padrão de uma cotação exibida (segundos).
QUOTE_TTL_SECONDS = 1800

#: Config da tarifa comercial temporária usada com a flag de cotação
#: agrupada desligada (SHIPPING_GROUPED_QUOTE_ENABLED=False). Não é cotação
#: por CEP: é o valor assumido pela loja até existir fonte real.
SHIPPING_TEMPORARY_FLAT_RATE_CFG = "SHIPPING_TEMPORARY_FLAT_RATE"

_CEP_RE = re.compile(r"^\d{8}$")


def normalize_cep(value) -> Optional[str]:
    """Normaliza CEP para 8 dígitos ou retorna None."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if _CEP_RE.match(digits) else None


def temporary_flat_rate() -> Decimal:
    """Tarifa comercial temporária (R$ 10,00 por padrão).

    Usada somente com ``SHIPPING_GROUPED_QUOTE_ENABLED=False``. É uma regra
    comercial assumida pela loja — não é cálculo por CEP, não considera
    fornecedor/distância nem oferece frete grátis. Pode divergir do custo
    real do fornecedor. Requer contexto da aplicação.

    Validação estrita: precisa ser decimal válido, finito, ``>= 0`` e
    arredondado em duas casas (``ROUND_HALF_UP``). Configuração inválida
    levanta ``QuoteUnavailable("invalid_quote")`` de forma sanitizada —
    nunca vira frete zero.
    """
    from decimal import InvalidOperation

    from flask import current_app

    raw = current_app.config.get(SHIPPING_TEMPORARY_FLAT_RATE_CFG, "10.00")
    if raw is None:
        raise QuoteUnavailable("invalid_quote")
    raw_str = str(raw).strip()
    if not raw_str:
        raise QuoteUnavailable("invalid_quote")
    try:
        value = Decimal(raw_str)
    except (InvalidOperation, ValueError, AttributeError, TypeError):
        raise QuoteUnavailable("invalid_quote") from None
    if not value.is_finite():
        raise QuoteUnavailable("invalid_quote")
    if value < 0:
        raise QuoteUnavailable("invalid_quote")
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


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
    origin_cep: object = None


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

    A tarifa é indexada pelo par (prefixo de origem, prefixo de destino):
    só responde combinações presentes na tabela, para a origem efetiva da
    remessa (`group.origin_cep`). Sem origem na remessa ou sem entrada para
    o par, falha fechado. NÃO inventa tarifa por distância nem aplica uma
    origem única a todos os fornecedores. Em produção, sem configuração, o
    checkout bloqueia (mensagem + retry) em vez de cobrar valor presumido.
    """

    def __init__(self, rates: dict):
        self.rates = rates or {}

    def quote(self, group: ShipmentGroup, dest_cep: str, now=None) -> Quote:  # noqa: ARG002
        dest = normalize_cep(dest_cep)
        if dest is None:
            raise QuoteUnavailable("invalid_destination")
        origin = normalize_cep(group.origin_cep)
        if origin is None:
            raise QuoteUnavailable("missing_origin")
        entry = self.rates.get((origin[:3], dest[:3]))
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
    """Classifica um item em ("aliexpress", supplier_id),
    ("domestic", supplier_id|None) ou ("unknown", None).
    Requer contexto da aplicação.

    Produto nacional vai para grupo doméstico com o fornecedor manual
    identificável como chave (ou None = estoque próprio). Produto
    AliExpress sem fornecedor identificável (vínculo ausente, ambíguo ou
    inválido) é "unknown" e bloqueia o fluxo — nunca é classificado
    levianamente como envio nacional.
    """
    from loja.modules.models import Product, ProductVariant, Supplier, SupplierProduct

    variant_supplier = None
    if variant_id is not None:
        variant = ProductVariant.query.filter_by(id=variant_id).first()
        if (
            variant is not None
            and variant.is_active
            and variant.supplier_product_id
        ):
            sp = SupplierProduct.query.filter_by(
                id=variant.supplier_product_id
            ).first()
            if (
                sp is not None
                and sp.is_active
                and sp.supplier_id is not None
            ):
                supplier = Supplier.query.filter_by(id=sp.supplier_id).first()
                if supplier is not None and _supplier_is_aliexpress(supplier):
                    return "aliexpress", supplier.id
                if supplier is not None:
                    # Vínculo ativo e fornecedor válido: pode determinar origem.
                    variant_supplier = supplier.id
            # Se o vínculo da variante está inativo/ausente/inválido, ignora
            # e reavalia os vínculos ativos do produto.
    product = (
        Product.query.filter_by(id=product_id).first()
        if product_id is not None
        else None
    )
    if product is not None:
        actives = SupplierProduct.query.filter_by(
            product_id=product.id, is_active=True
        ).all()
        valid_ali = set()
        manual = set()
        for sp in actives:
            if sp.supplier_id is None:
                continue
            supplier = Supplier.query.filter_by(id=sp.supplier_id).first()
            if _supplier_is_aliexpress(supplier):
                valid_ali.add(supplier.id)
            elif supplier is not None:
                manual.add(supplier.id)
        if len(valid_ali) == 1:
            return "aliexpress", next(iter(valid_ali))
        if getattr(product, "source", None) != "aliexpress":
            # Variante vinculada inequivocamente vence, mesmo com outros vínculos.
            if variant_supplier is not None:
                return "domestic", variant_supplier
            if len(manual) == 1:
                return "domestic", next(iter(manual))
            if len(manual) == 0:
                # Sem vínculo: estoque próprio comprovado.
                return "domestic", None
            # Dois ou mais fornecedores manuais ativos, sem variante que
            # determine inequivocamente -> origem desconhecida (fail-closed).
            return "unknown", None
    return "unknown", None


def rates_from_config(raw) -> dict:
    """Tabela doméstica a partir da config; formato inválido vira {} (bloqueio).

    Chaves são pares (prefixo_origem, prefixo_destino); valores
    [custo, min_dias, max_dias]. Chaves vindas de JSON (listas) são
    normalizadas para tuplas.
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, str):
        import json

        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        items = parsed.items()
    else:
        return {}
    normalized = {}
    for prefixes, entry in items:
        try:
            origin_prefix, dest_prefix = (
                tuple(prefixes) if isinstance(prefixes, list) else prefixes
            )
            cost, min_days, max_days = entry
            normalized[(str(origin_prefix), str(dest_prefix))] = (
                to_decimal(cost),
                int(min_days),
                int(max_days),
            )
        except (TypeError, ValueError):
            continue
    return normalized


def resolve_group_origin(channel, key):
    """Origem efetiva da remessa (CEP) ou None.

    Doméstico: mapa ``SHIPPING_DOMESTIC_ORIGINS`` por fornecedor
    (chave str(supplier_id), ou "own" para estoque próprio); apenas o
    estoque próprio pode usar ``SHIPPING_ORIGIN_CEP``. Um único CEP nunca
    é aplicado a todos os fornecedores. Requer contexto da aplicação.
    """
    if channel != "domestic":
        return None
    from flask import current_app

    origins = current_app.config.get("SHIPPING_DOMESTIC_ORIGINS") or {}
    if not isinstance(origins, dict):
        origins = {}
    supplier_key = str(key) if key is not None else "own"
    origin = origins.get(supplier_key)
    if origin is None and key is None:
        origin = current_app.config.get("SHIPPING_ORIGIN_CEP")
    return normalize_cep(origin) if origin else None


def assign_group_origins(groups) -> list:
    """Preenche origin_cep de cada grupo (requer contexto da aplicação)."""
    from dataclasses import replace

    return [
        replace(g, origin_cep=resolve_group_origin(g.channel, g.key))
        for g in groups
    ]


def default_providers():
    """Provedores da operação: origem/tabela doméstica via config (sem padrão).

    Sem ``SHIPPING_ORIGIN_CEP`` ou sem tabela, o grupo nacional falha
    fechado (missing_origin/provider_unavailable) em vez de presumir valor.
    """
    from flask import current_app

    rates = rates_from_config(current_app.config.get("SHIPPING_DOMESTIC_RATES"))
    fresh = current_app.config.get("SHIPPING_ALIEXPRESS_FRESH_QUOTES") or {}
    if not isinstance(fresh, dict):
        fresh = {}
    return {
        "domestic": TableDomesticProvider(rates),
        "aliexpress": AliExpressSupplierProvider(fresh_quotes=fresh),
    }


def quote_for_items(item_dicts, dest_cep: str, now=None) -> dict:
    """Cota itens de checkout (dicts com product_id/variant_id/quantity).

    Origens por remessa são resolvidas da config (requer contexto).
    """
    lines = [
        ShipmentLine(
            product_id=item.get("product_id"),
            variant_id=item.get("variant_id"),
            quantity=int(item.get("quantity") or 1),
        )
        for item in item_dicts
    ]
    groups = assign_group_origins(group_lines(lines))
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
        # Chave inclui o fornecedor: remessas distintas nunca se fundem.
        # "unknown" tem key None e bloqueia adiante.
        bucket_key = (channel, str(key) if key is not None else None)
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
    "QUOTE_TTL_SECONDS",
    "SHIPPING_TEMPORARY_FLAT_RATE_CFG",
    "QuoteUnavailable",
    "ShipmentGroup",
    "ShipmentLine",
    "Quote",
    "normalize_cep",
    "temporary_flat_rate",
    "canonical_items",
    "estimate_label",
    "validate_quote",
    "rates_from_config",
    "assert_not_estimate",
    "TableDomesticProvider",
    "AliExpressSupplierProvider",
    "classify_line",
    "resolve_group_origin",
    "assign_group_origins",
    "default_providers",
    "quote_for_items",
    "group_lines",
    "quote_groups",
]
