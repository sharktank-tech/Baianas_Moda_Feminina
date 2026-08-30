"""Serviços de domínio da PolloModas (Fase 6).

Contratos financeiros canônicos:
    supplier cost = SupplierProduct.cost
    shipping cost = SupplierProduct.shipping_cost
    landed cost   = supplier cost + shipping cost
    selling price = Product.price

impostos / FX / gateway fee / parcelamento / desconto / comissão ficam FORA do
landed cost nesta fase — nenhum valor presumido é adicionado.

- pricing      : cálculo do landed cost e precificação de rascunhos AliExpress.
- publication  : guarda de publicação (evaluate/publish) com códigos estáveis.
"""

from . import pricing  # noqa: F401
from . import publication  # noqa: F401

__all__ = ["pricing", "publication"]