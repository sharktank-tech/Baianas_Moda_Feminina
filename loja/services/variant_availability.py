"""Disponibilidade comercial de variantes para venda (decisão do admin).

Separa a decisão comercial da disponibilidade do fornecedor: desabilitar
uma variante impede NOVAS compras sem apagar histórico (pedidos, reservas
e evidências permanecem intactos) e sem zerar estoque. Reativação é
EXPLÍCITA pelo mesmo caminho — importação e sincronização nunca reativam
(veja `importer` e `published_sync`, que só desativam por estoque zerado
ou SKU ausente).

Sem escrita em preço, custo, frete, origem ou publicação. Só faz `flush()`;
o commit/rollback é de quem chama.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from loja import db
from loja.modules.models import Product, ProductVariant


@dataclass(frozen=True)
class VariantAvailabilityResult:
    """Resultado da troca de disponibilidade de uma variante."""

    product_id: object
    variant_id: object
    is_active: bool
    stock_before: int
    stock_after: int


def parse_availability_flag(value) -> Optional[bool]:
    """Normaliza o campo `disponivel` do formulário (estrito, fail-closed)."""
    text = str(value).strip() if value is not None else ""
    if text == "1":
        return True
    if text == "0":
        return False
    return None


def set_variant_availability(
    *,
    product: Product,
    variant_id,
    available: bool,
) -> VariantAvailabilityResult:
    """Habilita/desabilita UMA variante para venda, com locks e agregado.

    Trava Product -> ProductVariant na MESMA ordem do checkout e recarrega
    (`populate_existing`): a decisão usa valores frescos. A variante precisa
    existir e pertencer ao produto, senão `ValueError` com código estável e
    nenhuma escrita. O agregado `Product.stock` é recalculado pela regra
    canônica (soma dos estoques das variantes ATIVAS do vínculo).
    """
    if product is None:
        raise ValueError("product_not_found")
    try:
        wanted_id = uuid.UUID(str(variant_id))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("variant_invalid") from None

    locked_product = (
        Product.query.filter_by(id=product.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if locked_product is None:
        raise ValueError("product_not_found")
    variant = (
        ProductVariant.query.filter_by(id=wanted_id, product_id=product.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if variant is None:
        raise ValueError("variant_not_in_product")

    stock_before = int(locked_product.stock or 0)
    variant.is_active = bool(available)

    query = ProductVariant.query.filter_by(product_id=product.id)
    if variant.supplier_product_id is not None:
        query = query.filter_by(supplier_product_id=variant.supplier_product_id)
    active_stocks = [
        max(stock or 0, 0)
        for stock, in query.filter_by(is_active=True)
        .with_entities(ProductVariant.stock)
        .all()
    ]
    locked_product.stock = sum(active_stocks)
    db.session.flush()

    return VariantAvailabilityResult(
        product_id=product.id,
        variant_id=variant.id,
        is_active=bool(variant.is_active),
        stock_before=stock_before,
        stock_after=int(locked_product.stock or 0),
    )


__all__ = [
    "VariantAvailabilityResult",
    "parse_availability_flag",
    "set_variant_availability",
]
