"""Testes financeiros de precificação dropshipping (Fase 6).

Contratos:
    margem  (%) = (preço_venda - landed_cost) / preço_venda * 100
    markup  (%) = (preço_venda - landed_cost) / landed_cost * 100
    preço_venda = landed_cost / (1 - M/100), arredondado SEMPRE para CIMA até o
    centavo (o arredondamento nunca reduz a margem alvo).

Nenhum cálculo usa float; apenas Decimal.
"""
import uuid

import pytest
from decimal import Decimal

from loja.integrations.aliexpress.importer import SOURCE_ALIEXPRESS
from loja.modules.models import Product, ProductVariant, Supplier, SupplierProduct
from loja.services import pricing
from loja.services.pricing import (
    PricingBlocked,
    PricingConflict,
    PricingError,
    calculate_landed_cost,
    compute_margin,
    compute_markup,
    price_dropshipping_product,
    sale_price_for_margin,
    to_decimal,
)


def _sp(
    product,
    *,
    cost="100.00",
    freight_status="available",
    shipping_cost="10.00",
    currency="BRL",
    freight_currency="BRL",
):
    return SupplierProduct(
        id=uuid.uuid4(),
        supplier_id=uuid.uuid4(),
        product_id=product.id,
        supplier_product_id="SP_" + uuid.uuid4().hex[:8],
        cost=Decimal(cost) if cost is not None else None,
        currency=currency,
        freight_status=freight_status,
        shipping_cost=Decimal(shipping_cost) if shipping_cost is not None else None,
        landed_cost=None,
        freight_currency=freight_currency,
        is_active=True,
    )


def _product(db, **overrides):
    kwargs = dict(
        id=uuid.uuid4(),
        name="Camiseta PG",
        price=Decimal("1.00"),
        stock=5,
        is_active=False,
        is_draft=True,
        source=SOURCE_ALIEXPRESS,
        external_id="1005" + uuid.uuid4().hex[:12],
        currency="BRL",
        pricing_status="unpriced",
    )
    kwargs.update(overrides)
    product = Product(**kwargs)
    db.session.add(product)
    db.session.flush()
    return product


def _ready(db, **sp_kwargs):
    product = _product(db)
    supplier_product = _sp(product, **sp_kwargs)
    db.session.add(supplier_product)
    # variante ativa com estoque (para publication), irrelevante para pricing
    db.session.add(ProductVariant(
        id=uuid.uuid4(),
        product_id=product.id,
        supplier_product_id=supplier_product.id,
        stock=5,
        is_active=True,
    ))
    db.session.flush()
    return product, supplier_product


def _run(db, product, supplier_product, margin):
    result = price_dropshipping_product(
        product=product, target_margin_percent=margin
    )
    db.session.commit()
    db.session.refresh(product)
    db.session.refresh(supplier_product)
    return result


# --------------------------------------------------------------------------
# helpers puros (Decimal, sem float)
# --------------------------------------------------------------------------

def test_to_decimal_normalizes_strings_and_nulls():
    assert to_decimal("100.50") == Decimal("100.50")
    assert to_decimal(Decimal("1.1")) == Decimal("1.1")
    assert to_decimal("abc") is None
    assert to_decimal(None) is None


def test_margin_and_markup_definitions():
    assert compute_margin(Decimal("125.00"), Decimal("100.00")) == Decimal("20.00")
    assert compute_markup(Decimal("125.00"), Decimal("100.00")) == Decimal("25.00")


def test_sale_price_for_margin_formula():
    assert sale_price_for_margin(Decimal("100.00"), Decimal("20")) == Decimal("125.00")


def test_compute_margin_and_markup_normalize_string_inputs():
    # Contrato Fase 6: ambos os operandos são normalizados (str é aceito).
    assert compute_margin("125.00", "100.00") == Decimal("20.00")
    assert compute_markup("125.00", "100.00") == Decimal("25.00")
    with pytest.raises(ValueError):
        compute_markup("125.00", None)  # landed inválido, não exploda com None


# --------------------------------------------------------------------------
# landed = 100 + margem 20% => preço 125.00, margem 20%, markup 25%
# --------------------------------------------------------------------------

def test_landed_100_margin_20_price_125(db):
    product, supplier_product = _ready(db, shipping_cost="0.00")
    result = _run(db, product, supplier_product, "20")

    assert result.landed_cost == Decimal("100.00")
    assert result.sale_price == Decimal("125.00")
    assert result.margin_percent == Decimal("20.00")
    assert result.markup_percent == Decimal("25.00")

    assert supplier_product.landed_cost == Decimal("100.00")
    assert product.price == Decimal("125.00")
    assert product.pricing_status == pricing.PRICED
    assert product.pricing_error is None
    assert product.target_margin_percent == Decimal("20.00")
    # pricing != publicação: continua draft e inativo
    assert product.is_draft is True
    assert product.is_active is False


# --------------------------------------------------------------------------
# arredondamento para cima nunca reduz a margem alvo
# --------------------------------------------------------------------------

def test_rounds_up_to_next_cent_never_reduces_margin(db):
    product, supplier_product = _ready(db, cost="99.99", shipping_cost="0.00")
    result = _run(db, product, supplier_product, "30")

    # 99.99 / 0.7 = 142.842857... -> ROUND_UP -> 142.85
    assert result.sale_price == Decimal("142.85")
    assert result.landed_cost == Decimal("99.99")
    assert result.margin_percent >= Decimal("30.00")


def test_shipping_zero_confirmed_is_accepted(db):
    product, supplier_product = _ready(db, shipping_cost="0.00")
    result = _run(db, product, supplier_product, "20")
    assert result.landed_cost == Decimal("100.00")
    assert result.sale_price == Decimal("125.00")


# --------------------------------------------------------------------------
# margens inválidas
# --------------------------------------------------------------------------

@pytest.mark.parametrize("margin", ["0", "-5", "100", "150"])
def test_invalid_margins_are_rejected(db, margin):
    product, supplier_product = _ready(db)
    with pytest.raises(PricingBlocked) as excinfo:
        price_dropshipping_product(product=product, target_margin_percent=margin)
    assert excinfo.value.args[0] == "margin_invalid"


def test_margin_none_is_rejected(db):
    product, supplier_product = _ready(db)
    with pytest.raises(PricingBlocked) as excinfo:
        price_dropshipping_product(product=product, target_margin_percent=None)
    assert excinfo.value.args[0] == "margin_missing"


# --------------------------------------------------------------------------
# frete / custo / moeda
# --------------------------------------------------------------------------

def test_freight_provider_unavailable_blocks(db):
    product, supplier_product = _ready(db, freight_status="provider_unavailable", shipping_cost=None)
    with pytest.raises(PricingBlocked) as excinfo:
        price_dropshipping_product(product=product, target_margin_percent="20")
    assert excinfo.value.args[0] == "freight_unavailable"
    assert supplier_product.landed_cost is None


def test_shipping_cost_null_blocks(db):
    product, supplier_product = _ready(db, freight_status="available", shipping_cost=None)
    with pytest.raises(PricingBlocked) as excinfo:
        price_dropshipping_product(product=product, target_margin_percent="20")
    assert excinfo.value.args[0] == "shipping_cost_missing"


def test_supplier_cost_null_or_zero_blocks(db):
    for raw_cost in [None, "0", "0.00"]:
        product, supplier_product = _ready(db, cost=raw_cost)
        with pytest.raises(PricingBlocked) as excinfo:
            price_dropshipping_product(product=product, target_margin_percent="20")
        assert excinfo.value.args[0] == "supplier_cost_missing"
        assert supplier_product.landed_cost is None


def test_currency_mismatch_blocks(db):
    product, supplier_product = _ready(
        db, currency="BRL", freight_currency="USD"
    )
    with pytest.raises(PricingBlocked) as excinfo:
        price_dropshipping_product(product=product, target_margin_percent="20")
    assert excinfo.value.args[0] == "currency_mismatch"
    assert supplier_product.landed_cost is None


def test_product_currency_divergent_from_supplier_never_prices(db):
    # product.currency (BRL) != SupplierProduct.currency/frete (USD): mesmo com
    # fornecedor×frete coerentes, a moeda de VENDA diverge -> nunca `priced`.
    product, supplier_product = _ready(db, currency="USD", freight_currency="USD")
    with pytest.raises(PricingBlocked) as excinfo:
        price_dropshipping_product(product=product, target_margin_percent="20")
    assert excinfo.value.args[0] == "currency_mismatch"
    assert product.pricing_status == "unpriced"
    assert product.price == Decimal("1.00")
    assert supplier_product.landed_cost is None


def test_negative_shipping_blocks_at_service_level(db):
    # shipping < 0 é rejeitado pelo SERVIÇO (calculate_landed_cost), sem
    # depender apenas do CHECK de banco. Vínculo em memória: o CHECK nem roda.
    product = _product(db)
    supplier_product = SupplierProduct(
        id=uuid.uuid4(),
        supplier_id=uuid.uuid4(),
        product_id=product.id,
        supplier_product_id="SP_NEG",
        cost=Decimal("100.00"),
        currency="BRL",
        freight_status="available",
        shipping_cost=Decimal("-0.01"),
        freight_currency="BRL",
        is_active=True,
    )
    product.supplier_links = [supplier_product]
    with pytest.raises(PricingBlocked) as excinfo:
        price_dropshipping_product(product=product, target_margin_percent="20")
    assert excinfo.value.args[0] == "shipping_cost_invalid"
    assert supplier_product.landed_cost is None
    assert product.pricing_status == "unpriced"


def test_multiple_active_suppliers_ambiguous(db):
    product = _product(db)
    sp1 = _sp(product)
    sp2 = _sp(product)
    db.session.add_all([sp1, sp2])
    db.session.flush()

    with pytest.raises(PricingConflict) as excinfo:
        price_dropshipping_product(product=product, target_margin_percent="20")
    assert excinfo.value.args[0] == "supplier_ambiguous"


def test_no_active_supplier_blocks(db):
    product = _product(db)
    db.session.flush()
    with pytest.raises(PricingBlocked) as excinfo:
        price_dropshipping_product(product=product, target_margin_percent="20")
    assert excinfo.value.args[0] == "supplier_missing"


def test_only_aliexpress_products_can_be_priced(db):
    product = _product(db, source="internal")
    db.session.flush()
    with pytest.raises(PricingError):
        price_dropshipping_product(product=product, target_margin_percent="20")


# --------------------------------------------------------------------------
# calculate_landed_cost direto
# --------------------------------------------------------------------------

def test_calculate_landed_cost_rounds_to_cent(db):
    product, supplier_product = _ready(
        db, cost="10.001", shipping_cost="2.009"
    )
    assert calculate_landed_cost(supplier_product) == Decimal("12.01")