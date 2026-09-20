"""Cotação de frete por grupo de remessa/fornecedor (revisão comercial).

Sem chamadas reais: provedor doméstico simulado com origem + tabela
explícitas; AliExpress com dados persistidos do fornecedor. Em produção,
sem origem/tabela/custo válido, o checkout bloqueia (mensagem + retry)
em vez de cobrar tarifa presumida. R$ 10 é só estimativa pré-CEP.
"""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from loja.services.shipping_quote import (
    QUOTE_TTL_SECONDS,
    AliExpressSupplierProvider,
    Quote,
    QuoteUnavailable,
    ShipmentGroup,
    ShipmentLine,
    TableDomesticProvider,
    assert_not_estimate,
    estimate_label,
    group_lines,
    normalize_cep,
    quote_groups,
    validate_quote,
)
from loja.utils import utcnow


RATES = {
    "013": (Decimal("5.00"), 1, 2),
    "771": (Decimal("7.50"), 3, 5),
}
ORIGIN = "01310100"


def _domestic_provider(origin=ORIGIN, rates=None):
    return TableDomesticProvider(origin, rates if rates is not None else RATES)


def _providers(origin=ORIGIN, rates=None):
    return {
        "domestic": _domestic_provider(origin, rates),
        "aliexpress": AliExpressSupplierProvider(),
    }


def _group(channel="domestic", key=None, n=1):
    return ShipmentGroup(
        key=key,
        channel=channel,
        lines=tuple(
            ShipmentLine(product_id=uuid.uuid4(), quantity=1) for _ in range(n)
        ),
    )


# ── Normalização e estimativa ───────────────────────────────────────────

def test_normalize_cep():
    assert normalize_cep("01310-100") == "01310100"
    assert normalize_cep("123") is None
    assert normalize_cep(None) is None


def test_estimate_label_nunca_e_cobranca():
    assert "estimativa" in estimate_label()
    with pytest.raises(QuoteUnavailable):
        assert_not_estimate(Decimal("10.00"))
    with pytest.raises(QuoteUnavailable):
        assert_not_estimate("10.00")
    # Valor real igual por coincidência num Quote passa (é cotação, não rótulo).
    assert_not_estimate(
        Quote(cost=Decimal("10.00"), quoted_at=utcnow())
    )


def test_validate_quote_rejeita_vencida():
    velha = Quote(
        cost=Decimal("5.00"),
        quoted_at=utcnow() - timedelta(seconds=QUOTE_TTL_SECONDS + 60),
    )
    with pytest.raises(QuoteUnavailable) as exc:
        validate_quote(velha)
    assert exc.value.code == "expired"
    fresca = Quote(cost=Decimal("5.00"), quoted_at=utcnow())
    assert validate_quote(fresca).cost == Decimal("5.00")


# ── Provedor doméstico simulado ─────────────────────────────────────────

def test_ceps_distintos_cotacoes_distintas():
    providers = _providers()
    r1 = quote_groups([_group()], "01310100", providers)
    r2 = quote_groups([_group()], "77100000", providers)
    assert r1["total"] == Decimal("5.00")
    assert r2["total"] == Decimal("7.50")


def test_origens_distintas_cotacoes_distintas():
    dest = "77100000"
    a = quote_groups([_group()], dest, _providers(origin="01310100"))
    b = quote_groups([_group()], dest, _providers(origin="77100000"))
    # Mesma tabela, origens diferentes: o provedor usa a origem configurada.
    assert a["groups"][0]["carrier"] == "simulated-domestic"
    assert b["groups"][0]["carrier"] == "simulated-domestic"
    assert a["total"] == b["total"] == Decimal("7.50")


def test_sem_origem_bloqueia():
    with pytest.raises(QuoteUnavailable) as exc:
        quote_groups([_group()], "01310100", _providers(origin=None))
    assert exc.value.code == "missing_origin"


def test_prefixo_desconhecido_bloqueia():
    with pytest.raises(QuoteUnavailable) as exc:
        quote_groups([_group()], "99999999", _providers())
    assert exc.value.code == "provider_unavailable"


def test_destino_invalido_bloqueia():
    with pytest.raises(QuoteUnavailable) as exc:
        quote_groups([_group()], "abc", _providers())
    assert exc.value.code == "invalid_destination"


def test_dois_grupos_somam_e_mostram_prazos():
    groups = [
        ShipmentGroup(key=None, channel="domestic", lines=(ShipmentLine(product_id=uuid.uuid4()),)),
        ShipmentGroup(key=uuid.uuid4(), channel="domestic", lines=(ShipmentLine(product_id=uuid.uuid4()),)),
    ]
    providers = {
        "domestic": TableDomesticProvider(ORIGIN, {"013": (Decimal("5.00"), 1, 2)}),
        "other": TableDomesticProvider(ORIGIN, {"013": (Decimal("7.00"), 4, 9)}),
    }
    groups[1] = ShipmentGroup(key=groups[1].key, channel="other", lines=groups[1].lines)
    result = quote_groups(groups, "01310100", providers)
    assert result["total"] == Decimal("12.00")
    prazos = {(g["min_days"], g["max_days"]) for g in result["groups"]}
    assert prazos == {(1, 2), (4, 9)}


def test_grupo_sem_provedor_bloqueia_tudo():
    with pytest.raises(QuoteUnavailable):
        quote_groups([_group(channel="inexistente")], "01310100", _providers())


def test_arredondamento_decimal():
    providers = {
        "domestic": TableDomesticProvider(ORIGIN, {"013": (Decimal("5.005"), 1, 2)}),
        "aliexpress": AliExpressSupplierProvider(),
    }
    result = quote_groups([_group()], "01310100", providers)
    assert result["total"] == Decimal("5.01")


# ── Provedor AliExpress (dados persistidos) ─────────────────────────────

def _supplier_link(db, *, freight_status="available", shipping="4.00",
                   currency="BRL", freight_currency="BRL", cost="10.00"):
    from loja.modules.models import (
        Product,
        ProductVariant,
        Supplier,
        SupplierProduct,
    )

    supplier = Supplier(
        id=uuid.uuid4(), name="Supp", channel_type="aliexpress",
        seller_id="SELLER_Q",
    )
    db.session.add(supplier)
    db.session.flush()
    product = Product(
        id=uuid.uuid4(), name="Prod AE", price=Decimal("29.90"),
        source="aliexpress", external_id="EXT_Q", is_draft=True,
        is_active=False,
    )
    db.session.add(product)
    db.session.flush()
    sp = SupplierProduct(
        id=uuid.uuid4(), supplier_id=supplier.id, product_id=product.id,
        supplier_product_id="SP_Q",
        cost=Decimal(cost) if cost is not None else None,
        currency=currency, freight_status=freight_status,
        shipping_cost=Decimal(shipping) if shipping is not None else None,
        freight_currency=freight_currency, is_active=True,
    )
    db.session.add(sp)
    db.session.flush()
    variant = ProductVariant(
        id=uuid.uuid4(), product_id=product.id,
        supplier_product_id=sp.id, supplier_sku_id="SKU_Q",
        stock=5, is_active=True,
    )
    db.session.add(variant)
    db.session.commit()
    return product, variant


def test_aliexpress_usa_custo_do_fornecedor(db):
    product, variant = _supplier_link(db)
    group = ShipmentGroup(
        key=uuid.uuid4(), channel="aliexpress",
        lines=(ShipmentLine(product_id=product.id, variant_id=variant.id),),
    )
    quote = AliExpressSupplierProvider().quote(group, "01310100")
    assert quote.cost == Decimal("4.00")
    assert quote.min_days is None  # prazo não persistido: apresentar como do fornecedor


def test_aliexpress_frete_indisponivel_bloqueia(db):
    product, variant = _supplier_link(db, freight_status="provider_unavailable")
    group = ShipmentGroup(
        key=uuid.uuid4(), channel="aliexpress",
        lines=(ShipmentLine(product_id=product.id, variant_id=variant.id),),
    )
    with pytest.raises(QuoteUnavailable) as exc:
        AliExpressSupplierProvider().quote(group, "01310100")
    assert exc.value.code == "provider_unavailable"


def test_aliexpress_moeda_incompativel_bloqueia(db):
    product, variant = _supplier_link(db, currency="USD", freight_currency="USD")
    group = ShipmentGroup(
        key=uuid.uuid4(), channel="aliexpress",
        lines=(ShipmentLine(product_id=product.id, variant_id=variant.id),),
    )
    with pytest.raises(QuoteUnavailable) as exc:
        AliExpressSupplierProvider().quote(group, "01310100")
    assert exc.value.code == "currency_mismatch"


def test_aliexpress_sem_vinculo_bloqueia(db, make_product):
    product = make_product()
    group = ShipmentGroup(
        key=uuid.uuid4(), channel="aliexpress",
        lines=(ShipmentLine(product_id=product.id),),
    )
    with pytest.raises(QuoteUnavailable) as exc:
        AliExpressSupplierProvider().quote(group, "01310100")
    assert exc.value.code == "no_supplier_data"


def test_group_lines_separa_nacional_e_fornecedor(db, make_product):
    interno = make_product()
    ae_product, ae_variant = _supplier_link(db)
    from loja.services.shipping_quote import resolve_supplier_id

    groups = group_lines(
        [
            ShipmentLine(product_id=interno.id),
            ShipmentLine(product_id=ae_product.id, variant_id=ae_variant.id),
        ],
        lambda line: resolve_supplier_id(line.product_id, line.variant_id),
    )
    assert {g.channel for g in groups} == {"domestic", "aliexpress"}


# ── Integração: /calcular_frete e criação do pedido ─────────────────────

def _rates_config(monkeypatch, client):
    monkeypatch.setitem(client.application.config, "SHIPPING_ORIGIN_CEP", ORIGIN)
    monkeypatch.setitem(
        client.application.config,
        "SHIPPING_DOMESTIC_RATES",
        {"013": (Decimal("5.00"), 1, 2), "771": (Decimal("7.50"), 3, 5)},
    )


def _login(client, user):
    client.post(
        "/login",
        data={"email": user.email, "password": "senha123"},
        follow_redirects=True,
    )


def test_calcular_frete_ceps_distintos(client, db, make_user, make_product, monkeypatch):
    _login(client, make_user())
    _rates_config(monkeypatch, client)
    product = make_product()

    r1 = client.post(
        "/calcular_frete",
        json={"cep": "01310100", "product_id": str(product.id), "quantity": 1},
    )
    r2 = client.post(
        "/calcular_frete",
        json={"cep": "77100000", "product_id": str(product.id), "quantity": 1},
    )
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.get_json()["valor"] == "5.00"
    assert r2.get_json()["valor"] == "7.50"
    assert r1.get_json()["grupos"][0]["carrier"] == "simulated-domestic"
    assert "expires_at" in r1.get_json()["grupos"][0]


def test_calcular_frete_sem_origem_bloqueia(client, db, make_user, make_product):
    _login(client, make_user())
    product = make_product()
    resp = client.post(
        "/calcular_frete",
        json={"cep": "01310100", "product_id": str(product.id), "quantity": 1},
    )
    assert resp.status_code == 409
    assert "indisponível" in resp.get_json()["error"]


def test_checkout_sem_cotacao_nao_cria_pedido(
    client, db, make_user, make_product, monkeypatch
):
    import loja.views.payments_routes as pr
    from loja.modules.getway_mercado_pago.payment_models import Order

    user = make_user()
    product = make_product(stock=5)
    _login(client, user)
    chamadas = []
    monkeypatch.setattr(
        pr, "create_preference", lambda order: chamadas.append(order) or {
            "preference_id": "pref-x", "checkout_url": "https://mp.example/x",
        },
    )
    antes = Order.query.count()
    resp = client.post(
        "/pagamentos/mercado-pago",
        data={
            "checkout_type": "product", "product_id": str(product.id),
            "quantity": 1, "selected_size": "M", "nome": "Cliente Teste",
            "email": user.email, "cep": "01310100", "estado": "SP",
            "endereco": "Rua Teste", "numero": "10", "bairro": "Centro",
            "cidade": "São Paulo",
        },
        follow_redirects=True,
    )
    assert "indisponível" in resp.get_data(as_text=True).lower()
    assert Order.query.count() == antes
    assert chamadas == []


def test_checkout_cotacao_cupom_e_preferencia_consistentes(
    client, db, make_user, make_product, make_coupon, monkeypatch
):
    import loja.views.payments_routes as pr
    from loja.modules.getway_mercado_pago.payment_models import Order

    user = make_user()
    product = make_product(stock=5)
    _login(client, user)
    _rates_config(monkeypatch, client)
    coupon = make_coupon(code="FRETE10", discount_type="fixed", discount_value="4")
    capturados = []
    monkeypatch.setattr(
        pr, "create_preference",
        lambda order: capturados.append(order) or {
            "preference_id": "pref-y", "checkout_url": "https://mp.example/y",
        },
    )
    resp = client.post(
        "/pagamentos/mercado-pago",
        data={
            "checkout_type": "product", "product_id": str(product.id),
            "quantity": 1, "selected_size": "M", "coupon_code": coupon.code,
            "nome": "Cliente Teste", "email": user.email, "cep": "01310100",
            "estado": "SP", "endereco": "Rua Teste", "numero": "10",
            "bairro": "Centro", "cidade": "São Paulo",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    order = Order.query.filter_by(user_id=user.id).one()
    # subtotal 49.90 (make_product) + frete 5.00 (prefixo 013) - cupom 4.00
    assert order.shipping == Decimal("5.00")
    assert order.total == Decimal("49.90") + Decimal("5.00") - Decimal("4.00")
    assert len(capturados) == 1
    assert float(capturados[0].total) == float(order.total)


def test_checkout_mudanca_endereco_recalcula(
    client, db, make_user, make_product, monkeypatch
):
    import loja.views.payments_routes as pr
    from loja.modules.getway_mercado_pago.payment_models import Order

    user = make_user()
    product = make_product(stock=5)
    _login(client, user)
    _rates_config(monkeypatch, client)
    def _pref(order, _n=[0]):
        _n[0] += 1
        return {"preference_id": f"pref-z-{_n[0]}", "checkout_url": "https://mp.example/z"}

    monkeypatch.setattr(pr, "create_preference", _pref)
    base = {
        "checkout_type": "product", "product_id": str(product.id),
        "quantity": 1, "selected_size": "M", "nome": "Cliente Teste",
        "email": user.email, "estado": "SP", "endereco": "Rua Teste",
        "numero": "10", "bairro": "Centro", "cidade": "São Paulo",
    }
    client.post("/pagamentos/mercado-pago", data={**base, "cep": "01310100"})
    client.post("/pagamentos/mercado-pago", data={**base, "cep": "77100000"})
    fretes = sorted(
        o.shipping for o in Order.query.filter_by(user_id=user.id).all()
    )
    assert fretes == [Decimal("5.00"), Decimal("7.50")]


def test_checkout_cotacao_vencida_ou_divergente_bloqueia(
    client, db, make_user, make_product, monkeypatch
):
    import loja.views.payments_routes as pr
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.utils import utcnow

    user = make_user()
    product = make_product(stock=5)
    _login(client, user)
    _rates_config(monkeypatch, client)
    monkeypatch.setattr(
        pr, "create_preference",
        lambda order: {"preference_id": "pref-w", "checkout_url": "https://mp.example/w"},
    )
    base = {
        "checkout_type": "product", "product_id": str(product.id),
        "quantity": 1, "selected_size": "M", "nome": "Cliente Teste",
        "email": user.email, "cep": "01310100", "estado": "SP",
        "endereco": "Rua Teste", "numero": "10", "bairro": "Centro",
        "cidade": "São Paulo",
    }
    antes = Order.query.count()
    # Cotação vencida (ontem).
    vencida = (utcnow() - timedelta(days=1)).isoformat()
    resp = client.post(
        "/pagamentos/mercado-pago",
        data={**base, "shipping_quoted": "5.00", "shipping_quoted_at": vencida},
        follow_redirects=True,
    )
    assert "vencida" in resp.get_data(as_text=True).lower()
    # Cotação divergente do recálculo.
    resp = client.post(
        "/pagamentos/mercado-pago",
        data={**base, "shipping_quoted": "1.00",
              "shipping_quoted_at": utcnow().isoformat()},
        follow_redirects=True,
    )
    assert "atualizado" in resp.get_data(as_text=True).lower()
    assert Order.query.count() == antes


# ── Vitrine: estimativa rotulada, nunca cobrança ─────────────────────────

def test_carrinho_mostra_estimativa_sem_cep(client, db, make_user, make_product):
    from loja.modules.models import Cart, CartItem

    user = make_user()
    product = make_product(stock=5)
    _login(client, user)
    cart = Cart(id=uuid.uuid4(), user_id=user.id)
    db.session.add(cart)
    db.session.flush()
    db.session.add(CartItem(
        id=uuid.uuid4(), cart_id=cart.id, product_id=product.id,
        selected_size="M", quantity=1, unit_price=product.price,
    ))
    db.session.commit()
    html = client.get("/cart").get_data(as_text=True)
    assert "a partir de R$ 10,00 (estimativa)" in html
    assert "18,90" not in html
    assert "249" not in html


def test_checkout_sem_cep_mostra_estimativa(client, db, make_user, make_product):
    user = make_user()
    product = make_product(stock=5)
    _login(client, user)
    with client.session_transaction() as sess:
        sess["checkout_product"] = {
            "product_id": str(product.id), "size": "M", "quantity": 1,
        }
    html = client.get(f"/checkout/{product.id}").get_data(as_text=True)
    assert "a partir de R$ 10,00 (estimativa)" in html
    assert "18,90" not in html


def test_cupom_json_sem_cep_traz_frete_nulo(client, db, make_user, make_product, make_coupon):
    user = make_user()
    product = make_product(stock=5)
    _login(client, user)
    coupon = make_coupon(code="CUPOM5", discount_type="fixed", discount_value="5")
    resp = client.post(
        "/checkout/aplicar-cupom",
        json={"checkout_type": "product", "product_id": str(product.id),
              "quantity": 1, "code": coupon.code},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["shipping"] is None
