"""Controles canônicos de NOVA compra sobre produto AliExpress.

Dois motivos estáveis impedem uma nova compra quando o produto já está à
venda, independentemente de onde o fluxo de checkout é iniciado:

1. DIVERGÊNCIA DE CUSTO/MOEDA (``supplier_cost_changed`` /
   ``currency_changed``): sinalizada pela sincronização técnica do produto
   publicado (``loja.integrations.aliexpress.published_sync``) em
   ``sync_error`` e aplicada à precificação (``pricing_status='stale'``).
   A aprovação operacional deixa de valer e o checkout não reserva estoque
   até o administrador renovar/reprecificar. Preço comercial, margem e
   conteúdo NUNCA são alterados por aqui.

2. OPERAÇÃO NACIONAL SEM ORIGEM COMPROVADA, avaliada POR VARIANTE: só a
   variante comprada e selecionada (``national_shipping_operation``) entra
   na conta. Evidência removida, exterior ou conflito manual × API
   bloqueiam a compra daquela variante; variante não selecionada nunca
   bloqueia.

Este módulo é somente leitura: nunca escreve em ``sync_error``,
``pricing_status`` nem em origem de envio. Mensagens são amigáveis e
estáveis por código — nenhuma lógica depende de texto livre.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

#: Códigos de divergência canônicos (escritos apenas pela sincronização).
DIVERGENCE_COST = "supplier_cost_changed"
DIVERGENCE_CURRENCY = "currency_changed"
DIVERGENCE_CODES = (DIVERGENCE_COST, DIVERGENCE_CURRENCY)

#: Código de divergência -> código canônico de leitura/publicação.
DIVERGENCE_READINESS_CODES = {
    DIVERGENCE_COST: "supplier_cost_divergence",
    DIVERGENCE_CURRENCY: "supplier_currency_divergence",
}

PURCHASE_BLOCK_MESSAGES = {
    DIVERGENCE_COST: (
        "Este produto está com divergência de custo com o fornecedor e "
        "precisa de revisão antes de novas compras."
    ),
    DIVERGENCE_CURRENCY: (
        "Este produto está com divergência de moeda com o fornecedor e "
        "precisa de revisão antes de novas compras."
    ),
    "national_origin_unproven": (
        "A opção escolhida ainda não tem origem de envio nacional comprovada."
    ),
    "national_origin_foreign": (
        "A opção escolhida é enviada do exterior e não faz parte da "
        "operação nacional."
    ),
    "national_origin_conflict": (
        "A origem de envio da opção escolhida está em conflito e precisa "
        "de revisão."
    ),
}


def supplier_divergence_code(product) -> Optional[str]:
    """Código de divergência de custo/moeda pendente no produto (ou None)."""
    if product is None:
        return None
    if getattr(product, "sync_error", None) in DIVERGENCE_CODES:
        return product.sync_error
    for link in getattr(product, "supplier_links", None) or []:
        if not getattr(link, "is_active", False):
            continue
        if getattr(link, "sync_error", None) in DIVERGENCE_CODES:
            return link.sync_error
    return None


def national_origin_code(variant) -> Optional[str]:
    """Bloqueio de origem da VARIANTE comprada quando ela participa do gate.

    Variante sem seleção de operação nacional nunca bloqueia por aqui
    (catálogo internacional segue vendável com a semântica já existente).
    """
    if variant is None:
        return None
    if not getattr(variant, "national_shipping_operation", False):
        return None

    from .shipping_origin import (
        ORIGIN_FOREIGN,
        REASON_NATIONAL_CONFLICT,
        REASON_NATIONAL_FOREIGN,
        REASON_NATIONAL_UNPROVEN,
        resolve_variant_origin,
    )

    resolution = resolve_variant_origin(variant)
    if resolution.conflict:
        return REASON_NATIONAL_CONFLICT
    if resolution.origin == ORIGIN_FOREIGN:
        return REASON_NATIONAL_FOREIGN
    if not resolution.known:
        return REASON_NATIONAL_UNPROVEN
    return None


def purchase_block_codes(product, variant=None) -> List[str]:
    """Códigos canônicos que impedem NOVA compra (lista pode ser vazia)."""
    codes: List[str] = []
    divergence = supplier_divergence_code(product)
    if divergence:
        codes.append(divergence)
    origin_code = national_origin_code(variant)
    if origin_code:
        codes.append(origin_code)
    return codes


def purchase_block_reason(
    product, variant=None
) -> Optional[Tuple[str, str]]:
    """(código, mensagem amigável) que proíbe nova compra, ou None."""
    codes = purchase_block_codes(product, variant)
    if not codes:
        return None
    code = codes[0]
    return code, PURCHASE_BLOCK_MESSAGES.get(
        code, "Este produto não está disponível para novas compras."
    )


def assert_purchase_allowed(product, variant=None, *, product_name=None) -> None:
    """Levanta ``ValueError`` quando a NOVA compra é proibida.

    Os dois caminhos canônicos de checkout (validação do carrinho e
    ``_reserve_stock``) já tratam ``ValueError`` como recusa amigável.
    """
    reason = purchase_block_reason(product, variant)
    if reason is None:
        return
    code, message = reason
    if product_name and code.startswith("national_origin_"):
        message = f"{product_name}: {message}"
    raise ValueError(message)


__all__ = [
    "DIVERGENCE_COST",
    "DIVERGENCE_CURRENCY",
    "DIVERGENCE_CODES",
    "DIVERGENCE_READINESS_CODES",
    "PURCHASE_BLOCK_MESSAGES",
    "assert_purchase_allowed",
    "national_origin_code",
    "purchase_block_codes",
    "purchase_block_reason",
    "supplier_divergence_code",
]
