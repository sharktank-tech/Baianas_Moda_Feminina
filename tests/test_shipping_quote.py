"""Cotação de frete por grupo de remessa/fornecedor (revisão comercial).

Sem chamadas reais: provedor doméstico simulado com origem + tabela
explícitas; AliExpress com dados persistidos do fornecedor. Em produção,
sem origem/tabela/custo válido, o checkout bloqueia (mensagem + retry)
em vez de cobrar tarifa presumida. Com flag desligada a loja cobra a
tarifa temporária ``SHIPPING_TEMPORARY_FLAT_RATE`` (validada, arredondada),
não estimativa; com flag ligada, pré-CEP é só rótulo.
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
    ("013", "013"): (Decimal("5.00"), 1, 2),
    ("013", "771"): (Decimal("7.50"), 3, 5),
    ("771", "771"): (Decimal("9.00"), 2, 4),
}
ORIGIN = "01310100"


def _providers(rates=None):
    return {
        "domestic": TableDomesticProvider(rates if rates is not None else RATES),
        "aliexpress": AliExpressSupplierProvider(),
    }


def _group(channel="domestic", key=None, n=1, origin=ORIGIN):
    return ShipmentGroup(
        key=key,
        channel=channel,
        lines=tuple(
            ShipmentLine(product_id=uuid.uuid4(), quantity=1) for _ in range(n)
        ),
        origin_cep=origin,
    )


# ── Normalização e estimativa ───────────────────────────────────────────

def test_normalize_cep():
    assert normalize_cep("01310-100") == "01310100"
    assert normalize_cep("123") is None
    assert normalize_cep(None) is None


def test_estimate_label_nao_sugere_tarifa():
    assert estimate_label() == "Frete calculado após informar o CEP"
    assert "10,00" not in estimate_label()


def test_rotulo_exibicao_nunca_vira_cobranca():
    # Texto de vitrine não parseável como dinheiro é rejeitado no limite.
    with pytest.raises(QuoteUnavailable):
        assert_not_estimate(estimate_label())
    # Numéricos passam: a distinção real × estimativa é estrutural
    # (só totais de Quote chegam à cobrança), nunca pelo valor.
    assert_not_estimate(Decimal("10.00"))
    assert_not_estimate("10.00")
    assert_not_estimate(Quote(cost=Decimal("10.00"), quoted_at=utcnow()))


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


def test_origens_distintas_mesmo_destino_tarifas_distintas():
    # Mesma tabela, origens efetivas diferentes por remessa.
    dest = "77100000"
    a = quote_groups([_group(origin="01310100")], dest, _providers())
    b = quote_groups([_group(origin="77100000")], dest, _providers())
    assert a["total"] == Decimal("7.50")
    assert b["total"] == Decimal("9.00")


def test_sem_origem_na_remessa_bloqueia():
    with pytest.raises(QuoteUnavailable) as exc:
        quote_groups([_group(origin=None)], "01310100", _providers())
    assert exc.value.code == "missing_origin"


def test_par_origem_destino_sem_tarifa_bloqueia():
    with pytest.raises(QuoteUnavailable) as exc:
        quote_groups([_group()], "99999999", _providers())
    assert exc.value.code == "provider_unavailable"


def test_destino_invalido_bloqueia():
    with pytest.raises(QuoteUnavailable) as exc:
        quote_groups([_group()], "abc", _providers())
    assert exc.value.code == "invalid_destination"


def test_dois_grupos_somam_e_mostram_prazos():
    groups = [
        ShipmentGroup(key=None, channel="domestic", origin_cep="01310100",
                      lines=(ShipmentLine(product_id=uuid.uuid4()),)),
        ShipmentGroup(key=uuid.uuid4(), channel="domestic", origin_cep="77100000",
                      lines=(ShipmentLine(product_id=uuid.uuid4()),)),
    ]
    providers = {
        "domestic": TableDomesticProvider({
            ("013", "013"): (Decimal("5.00"), 1, 2),
            ("771", "013"): (Decimal("7.00"), 4, 9),
        }),
        "aliexpress": AliExpressSupplierProvider(),
    }
    result = quote_groups(groups, "01310100", providers)
    assert result["total"] == Decimal("12.00")
    prazos = {(g["min_days"], g["max_days"]) for g in result["groups"]}
    assert prazos == {(1, 2), (4, 9)}


def test_vencimento_total_e_o_minimo_dos_grupos():
    # Duas remessas com validades diferentes: o total vence na primeira.
    from loja.utils import utcnow as _now

    class _StubProvider:
        def __init__(self, quoted_at, ttl):
            self.quoted_at = quoted_at
            self.ttl = ttl

        def quote(self, group, dest_cep, now=None):
            from loja.services.shipping_quote import Quote as _Quote

            return _Quote(cost=Decimal("5.00"), carrier="stub",
                          quoted_at=self.quoted_at, ttl_seconds=self.ttl)

    now = _now()
    cedo = _StubProvider(now - timedelta(seconds=1700), 1800)
    tarde = _StubProvider(now, 3600)
    result = quote_groups(
        [_group(channel="domestic"), _group(channel="other")],
        "01310100",
        {"domestic": cedo, "other": tarde,
         "aliexpress": AliExpressSupplierProvider()},
    )
    expira = [g["expires_at"] for g in result["groups"]]
    assert min(expira) == cedo.quoted_at + timedelta(seconds=1800)
    assert max(expira) == tarde.quoted_at + timedelta(seconds=3600)
    assert min(expira) < max(expira)


def test_grupo_sem_provedor_bloqueia_tudo():
    with pytest.raises(QuoteUnavailable):
        quote_groups([_group(channel="inexistente")], "01310100", _providers())


def test_arredondamento_decimal():
    providers = {
        "domestic": TableDomesticProvider({("013", "013"): (Decimal("5.005"), 1, 2)}),
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


def _fresh(sp_id, *, cost="4.00", dest_prefix="013", option=None, qty=1,
           currency="BRL", quoted_at=None, ttl=QUOTE_TTL_SECONDS,
           min_days=2, max_days=6):
    from loja.utils import utcnow as _now

    return {
        str(sp_id): {
            "cost": Decimal(cost), "min_days": min_days, "max_days": max_days,
            "dest_prefix": dest_prefix, "option": option, "qty": qty,
            "currency": currency, "quoted_at": quoted_at or _now(),
            "ttl_seconds": ttl,
        }
    }


def _ae_group(product, variant, qty=1):
    return ShipmentGroup(
        key=uuid.uuid4(), channel="aliexpress",
        lines=(ShipmentLine(product_id=product.id, variant_id=variant.id,
                            quantity=qty),),
    )


def test_aliexpress_custo_fresco_comprovado_cobra(db):
    product, variant = _supplier_link(db)
    sp_id = variant.supplier_product_id
    quote = AliExpressSupplierProvider(
        fresh_quotes=_fresh(sp_id)
    ).quote(_ae_group(product, variant), "01310100")
    assert quote.cost == Decimal("4.00")
    assert (quote.min_days, quote.max_days) == (2, 6)


def test_aliexpress_sem_comprovacao_bloqueia(db):
    # Custo persistido existe (4.00), mas sem contexto fresco não há prova
    # de destino/opção/quantidade/momento: bloqueia em vez de assumir.
    product, variant = _supplier_link(db)
    with pytest.raises(QuoteUnavailable) as exc:
        AliExpressSupplierProvider().quote(_ae_group(product, variant), "01310100")
    assert exc.value.code == "unverified_cost"


def test_aliexpress_outro_cep_ou_quantidade_bloqueia(db):
    product, variant = _supplier_link(db)
    sp_id = variant.supplier_product_id
    provider = AliExpressSupplierProvider(fresh_quotes=_fresh(sp_id))
    # Mesmo grupo, outro destino: custo anterior não continua válido.
    with pytest.raises(QuoteUnavailable) as exc:
        provider.quote(_ae_group(product, variant), "77100000")
    assert exc.value.code == "unverified_cost"
    # Mesma cotação, outra quantidade: também bloqueia.
    with pytest.raises(QuoteUnavailable) as exc:
        provider.quote(_ae_group(product, variant, qty=2), "01310100")
    assert exc.value.code == "unverified_cost"


def test_aliexpress_cotacao_vencida_bloqueia(db):
    from loja.utils import utcnow as _now

    product, variant = _supplier_link(db)
    sp_id = variant.supplier_product_id
    velha = _now() - timedelta(seconds=QUOTE_TTL_SECONDS + 60)
    provider = AliExpressSupplierProvider(
        fresh_quotes=_fresh(sp_id, quoted_at=velha)
    )
    with pytest.raises(QuoteUnavailable) as exc:
        provider.quote(_ae_group(product, variant), "01310100")
    assert exc.value.code == "expired"


def test_aliexpress_moeda_incompativel_bloqueia(db):
    product, variant = _supplier_link(db)
    sp_id = variant.supplier_product_id
    provider = AliExpressSupplierProvider(
        fresh_quotes=_fresh(sp_id, currency="USD")
    )
    with pytest.raises(QuoteUnavailable) as exc:
        provider.quote(_ae_group(product, variant), "01310100")
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


def test_group_lines_separa_nacional_fornecedor_e_desconhecido(db, make_product):
    interno = make_product()
    ae_product, ae_variant = _supplier_link(db)
    # AliExpress sem nenhum vínculo de fornecedor: desconhecido, nunca nacional.
    from loja.modules.models import Product

    orphan = Product(
        id=uuid.uuid4(), name="Órfão", price=Decimal("19.90"), stock=3,
        is_active=False, is_draft=True, source="aliexpress",
        external_id="EXT_ORPHAN",
    )
    db.session.add(orphan)
    db.session.commit()

    groups = group_lines(
        [
            ShipmentLine(product_id=interno.id),
            ShipmentLine(product_id=ae_product.id, variant_id=ae_variant.id),
            ShipmentLine(product_id=orphan.id),
        ],
    )
    assert {g.channel for g in groups} == {"domestic", "aliexpress", "unknown"}
    from dataclasses import replace

    groups = [
        replace(g, origin_cep="01310100") if g.channel == "domestic" else g
        for g in groups
    ]
    providers = _providers()
    providers["aliexpress"] = AliExpressSupplierProvider(
        fresh_quotes=_fresh(ae_variant.supplier_product_id)
    )
    with pytest.raises(QuoteUnavailable) as exc:
        quote_groups(groups, "01310100", providers)
    assert exc.value.code == "unknown_supplier"


# ── Integração: /calcular_frete e criação do pedido ─────────────────────

def _rates_config(monkeypatch, client):
    monkeypatch.setitem(client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", True)
    monkeypatch.setitem(client.application.config, "SHIPPING_ORIGIN_CEP", ORIGIN)
    monkeypatch.setitem(
        client.application.config,
        "SHIPPING_DOMESTIC_RATES",
        {("013", "013"): (Decimal("5.00"), 1, 2),
         ("013", "771"): (Decimal("7.50"), 3, 5)},
    )


def _login(client, user):
    client.post(
        "/login",
        data={"email": user.email, "password": "senha123"},
        follow_redirects=True,
    )


def preset_shipping_quote(client, cep, items, shipping):
    """Registra na sessão a confirmação que o /calcular_frete geraria."""
    from loja.utils import utcnow

    with client.session_transaction() as sess:
        sess["shipping_quote"] = {
            "cep": cep,
            "items": [
                {
                    "product_id": str(item.get("product_id")),
                    "variant_id": (
                        str(item.get("variant_id"))
                        if item.get("variant_id")
                        else None
                    ),
                    "quantity": int(item.get("quantity") or 1),
                }
                for item in items
            ],
            "shipping": f"{Decimal(str(shipping)):.2f}",
            "quoted_at": utcnow().isoformat(),
            "expires_at": (
                utcnow() + timedelta(seconds=QUOTE_TTL_SECONDS)
            ).isoformat(),
        }


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


def test_calcular_frete_sem_origem_bloqueia(client, db, make_user, make_product, monkeypatch):
    _login(client, make_user())
    monkeypatch.setitem(client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", True)
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
    monkeypatch.setitem(client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", True)
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
    preset_shipping_quote(
        client, "01310100",
        [{"product_id": product.id, "quantity": 1}], "5.00",
    )
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
    preset_shipping_quote(
        client, "01310100",
        [{"product_id": product.id, "quantity": 1}], "5.00",
    )
    client.post("/pagamentos/mercado-pago", data={**base, "cep": "01310100"})
    preset_shipping_quote(
        client, "77100000",
        [{"product_id": product.id, "quantity": 1}], "7.50",
    )
    client.post("/pagamentos/mercado-pago", data={**base, "cep": "77100000"})
    fretes = sorted(
        o.shipping for o in Order.query.filter_by(user_id=user.id).all()
    )
    assert fretes == [Decimal("5.00"), Decimal("7.50")]


def test_checkout_fornecedor_cota_dez_reais_cria_pedido(
    client, db, make_user, monkeypatch
):
    # R$ 10,00 cotado de verdade pelo fornecedor é legítimo: a distinção
    # real × estimativa é estrutural (objetos Quote), nunca numérica.
    import loja.views.payments_routes as pr
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.utils import utcnow

    from loja.modules.models import Product as _Product

    user = make_user()
    product, variant = _supplier_link(db)
    sp_id = str(variant.supplier_product_id)
    # Checkout exige produto público; guarda exige priced+!draft p/ ativar.
    with db.session.no_autoflush:
        prod = db.session.get(_Product, product.id)
        prod.is_draft = False
        prod.pricing_status = "priced"
        prod.is_active = True
        db.session.commit()
    _login(client, user)
    monkeypatch.setitem(client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", True)
    monkeypatch.setitem(
        client.application.config,
        "SHIPPING_ALIEXPRESS_FRESH_QUOTES",
        {sp_id: {
            "cost": Decimal("10.00"), "min_days": 5, "max_days": 12,
            "dest_prefix": "013", "option": None, "qty": 1,
            "currency": "BRL", "quoted_at": utcnow(),
            "ttl_seconds": QUOTE_TTL_SECONDS,
        }},
    )
    preset_shipping_quote(
        client, "01310100",
        [{"product_id": product.id, "variant_id": variant.id, "quantity": 1}],
        "10.00",
    )
    capturados = []
    monkeypatch.setattr(
        pr, "create_preference",
        lambda order: capturados.append(order) or {
            "preference_id": "pref-10", "checkout_url": "https://mp.example/10",
        },
    )
    resp = client.post(
        "/pagamentos/mercado-pago",
        data={
            "checkout_type": "product", "product_id": str(product.id),
            "quantity": 1, "variant_id": str(variant.id),
            "selected_size": "M", "nome": "Cliente Teste",
            "email": user.email, "telefone": "11999999999",
            "cep": "01310100", "estado": "SP",
            "endereco": "Rua Teste", "numero": "10", "bairro": "Centro",
            "cidade": "São Paulo",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    order = Order.query.filter_by(user_id=user.id).one()
    assert order.shipping == Decimal("10.00")
    assert len(capturados) == 1


def _manual_supplier_cart(db, user, suffixes=("A", "B")):
    """Carrinho com dois fornecedores manuais distintos (duas remessas)."""
    from loja.modules.models import (
        Cart,
        CartItem,
        Product,
        Supplier,
        SupplierProduct,
    )

    cart = Cart(id=uuid.uuid4(), user_id=user.id)
    db.session.add(cart)
    db.session.flush()
    supplier_ids = []
    for suffix in suffixes:
        supplier = Supplier(
            id=uuid.uuid4(), name=f"Supp {suffix}", channel_type="manual",
        )
        db.session.add(supplier)
        db.session.flush()
        product = Product(
            id=uuid.uuid4(), name=f"Prod {suffix}", price=Decimal("20.00"),
            stock=5, is_active=True,
        )
        db.session.add(product)
        db.session.flush()
        db.session.add(SupplierProduct(
            id=uuid.uuid4(), supplier_id=supplier.id, product_id=product.id,
            supplier_product_id=f"SP_{suffix}", cost=Decimal("8.00"),
            currency="BRL", is_active=True,
        ))
        db.session.flush()
        db.session.add(CartItem(
            id=uuid.uuid4(), cart_id=cart.id, product_id=product.id,
            selected_size="M", quantity=1, unit_price=product.price,
        ))
        supplier_ids.append(supplier.id)
    db.session.commit()
    return cart, supplier_ids


def _cart_checkout_form(user, cep):
    return {
        "checkout_type": "cart", "nome": "Cliente Teste", "email": user.email,
        "cep": cep, "estado": "SP", "endereco": "Rua Teste", "numero": "10",
        "bairro": "Centro", "cidade": "São Paulo",
    }


def test_duas_origens_mesmo_destino_tarifas_distintas(
    client, db, make_user, monkeypatch
):
    user = make_user()
    _login(client, user)
    _, supplier_ids = _manual_supplier_cart(db, user)
    monkeypatch.setitem(client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", True)
    monkeypatch.setitem(
        client.application.config, "SHIPPING_DOMESTIC_ORIGINS",
        {str(supplier_ids[0]): "01310100", str(supplier_ids[1]): "77100000"},
    )
    monkeypatch.setitem(
        client.application.config, "SHIPPING_DOMESTIC_RATES",
        {("013", "771"): (Decimal("7.50"), 3, 5),
         ("771", "771"): (Decimal("9.00"), 2, 4)},
    )
    resp = client.post("/calcular_frete", json={"cart": True, "cep": "77100000"})
    assert resp.status_code == 200
    data = resp.get_json()
    custos = sorted(g["cost"] for g in data["grupos"])
    assert custos == ["7.50", "9.00"]
    assert data["valor"] == "16.50"


def test_falta_tarifa_para_uma_remessa_bloqueia_tudo(
    client, db, make_user, monkeypatch
):
    import loja.views.payments_routes as pr
    from loja.modules.getway_mercado_pago.payment_models import Order

    user = make_user()
    _login(client, user)
    _, supplier_ids = _manual_supplier_cart(db, user)
    monkeypatch.setitem(
        client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", True,
    )
    monkeypatch.setitem(
        client.application.config, "SHIPPING_DOMESTIC_ORIGINS",
        {str(supplier_ids[0]): "01310100", str(supplier_ids[1]): "77100000"},
    )
    # Tabela só cobre a primeira remessa.
    monkeypatch.setitem(
        client.application.config, "SHIPPING_DOMESTIC_RATES",
        {("013", "771"): (Decimal("7.50"), 3, 5)},
    )
    resp = client.post("/calcular_frete", json={"cart": True, "cep": "77100000"})
    assert resp.status_code == 409
    assert "indisponível" in resp.get_json()["error"]

    chamadas = []
    monkeypatch.setattr(
        pr, "create_preference",
        lambda order: chamadas.append(order) or {
            "preference_id": "pref-np", "checkout_url": "https://mp.example/np",
        },
    )
    antes = Order.query.count()
    resp = client.post(
        "/pagamentos/mercado-pago",
        data=_cart_checkout_form(user, "77100000"),
        follow_redirects=True,
    )
    assert "indisponível" in resp.get_data(as_text=True).lower()
    assert Order.query.count() == antes
    assert chamadas == []


def test_primeiro_vencimento_recusa_checkout(
    client, db, make_user, monkeypatch
):
    import loja.views.payments_routes as pr
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.utils import utcnow

    user = make_user()
    _login(client, user)
    _, supplier_ids = _manual_supplier_cart(db, user)
    monkeypatch.setitem(client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", True)
    monkeypatch.setitem(
        client.application.config, "SHIPPING_DOMESTIC_ORIGINS",
        {str(supplier_ids[0]): "01310100", str(supplier_ids[1]): "77100000"},
    )
    monkeypatch.setitem(
        client.application.config, "SHIPPING_DOMESTIC_RATES",
        {("013", "771"): (Decimal("7.50"), 3, 5),
         ("771", "771"): (Decimal("9.00"), 2, 4)},
    )
    resp = client.post("/calcular_frete", json={"cart": True, "cep": "77100000"})
    assert resp.status_code == 200
    assert len(resp.get_json()["grupos"]) == 2
    # Primeira remessa vence agora; a segunda continua válida.
    with client.session_transaction() as sess:
        quote = sess["shipping_quote"]
        quote["groups"][0]["expires_at"] = (
            utcnow() - timedelta(seconds=10)
        ).isoformat()
        sess["shipping_quote"] = quote
        sess.modified = True

    chamadas = []
    monkeypatch.setattr(
        pr, "create_preference",
        lambda order: chamadas.append(order) or {
            "preference_id": "pref-nv", "checkout_url": "https://mp.example/nv",
        },
    )
    antes = Order.query.count()
    resp = client.post(
        "/pagamentos/mercado-pago",
        data=_cart_checkout_form(user, "77100000"),
        follow_redirects=True,
    )
    assert "vencida" in resp.get_data(as_text=True).lower()
    assert Order.query.count() == antes
    assert chamadas == []


def test_checkout_sem_confirmacao_bloqueia(
    client, db, make_user, make_product, monkeypatch
):
    import loja.views.payments_routes as pr
    from loja.modules.getway_mercado_pago.payment_models import Order

    user = make_user()
    product = make_product(stock=5)
    _login(client, user)
    _rates_config(monkeypatch, client)
    chamadas = []
    monkeypatch.setattr(
        pr, "create_preference",
        lambda order: chamadas.append(order) or {
            "preference_id": "pref-w", "checkout_url": "https://mp.example/w",
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
    assert "calcule o frete" in resp.get_data(as_text=True).lower()
    assert Order.query.count() == antes
    assert chamadas == []


def test_checkout_confirmacao_vencida_ou_divergente_bloqueia(
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
    items = [{"product_id": product.id, "quantity": 1}]
    # Confirmação vencida (ontem).
    with client.session_transaction() as sess:
        sess["shipping_quote"] = {
            "cep": "01310100", "items": [
                {"product_id": str(product.id), "variant_id": None, "quantity": 1}
            ],
            "shipping": "5.00",
            "quoted_at": (utcnow() - timedelta(days=1)).isoformat(),
            "expires_at": (utcnow() - timedelta(hours=23)).isoformat(),
        }
    resp = client.post(
        "/pagamentos/mercado-pago", data=base, follow_redirects=True,
    )
    assert "vencida" in resp.get_data(as_text=True).lower()
    # Confirmação divergente do recálculo.
    preset_shipping_quote(client, "01310100", items, "1.00")
    resp = client.post(
        "/pagamentos/mercado-pago", data=base, follow_redirects=True,
    )
    assert "atualizado" in resp.get_data(as_text=True).lower()
    assert Order.query.count() == antes


def test_checkout_reconfirmacao_apos_mudanca_de_valor(
    client, db, make_user, make_product, monkeypatch
):
    # Simula alteração entre a exibição (cotação) e a preferência:
    # cliente viu frete 5.00, preço mudou, recálculo diverge da sessão.
    import loja.views.payments_routes as pr
    from loja.modules.getway_mercado_pago.payment_models import Order

    user = make_user()
    product = make_product(stock=5)
    _login(client, user)
    _rates_config(monkeypatch, client)
    chamadas = []
    monkeypatch.setattr(
        pr, "create_preference",
        lambda order: chamadas.append(order) or {
            "preference_id": "pref-v", "checkout_url": "https://mp.example/v",
        },
    )
    cotacao = client.post(
        "/calcular_frete",
        json={"cep": "01310100", "product_id": str(product.id), "quantity": 1},
    )
    assert cotacao.status_code == 200
    assert cotacao.get_json()["valor"] == "5.00"
    # Quantidade muda depois da exibição (itens passam a não conferir).
    antes = Order.query.count()
    resp = client.post(
        "/pagamentos/mercado-pago",
        data={
            "checkout_type": "product", "product_id": str(product.id),
            "quantity": 2, "selected_size": "M", "nome": "Cliente Teste",
            "email": user.email, "cep": "01310100", "estado": "SP",
            "endereco": "Rua Teste", "numero": "10", "bairro": "Centro",
            "cidade": "São Paulo",
        },
        follow_redirects=True,
    )
    assert "atualizado" in resp.get_data(as_text=True).lower()
    assert Order.query.count() == antes
    assert chamadas == []


# ── Vitrine: rótulo temporário (flag off) ou estimativa (flag on), nunca cobrança ─

def test_carrinho_sem_cep_flag_off_mostra_frete_padrao_temporario(client, db, make_user, make_product):
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
    assert "Frete padrão temporário" in html
    assert "10,00" not in html
    assert "18,90" not in html
    assert "249" not in html


def test_carrinho_sem_cep_flag_on_mostra_estimativa(client, db, make_user, make_product, monkeypatch):
    from loja.modules.models import Cart, CartItem

    user = make_user()
    product = make_product(stock=5)
    _login(client, user)
    monkeypatch.setitem(client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", True)
    cart = Cart(id=uuid.uuid4(), user_id=user.id)
    db.session.add(cart)
    db.session.flush()
    db.session.add(CartItem(
        id=uuid.uuid4(), cart_id=cart.id, product_id=product.id,
        selected_size="M", quantity=1, unit_price=product.price,
    ))
    db.session.commit()
    html = client.get("/cart").get_data(as_text=True)
    assert "Frete calculado após informar o CEP" in html
    assert "10,00" not in html
    assert "18,90" not in html
    assert "249" not in html


def test_checkout_sem_cep_mostra_frete_padrao_temporario(client, db, make_user, make_product):
    user = make_user()
    product = make_product(stock=5)
    _login(client, user)
    with client.session_transaction() as sess:
        sess["checkout_product"] = {
            "product_id": str(product.id), "size": "M", "quantity": 1,
        }
    html = client.get(f"/checkout/{product.id}").get_data(as_text=True)
    assert "Frete padrão temporário" in html
    assert "10,00" not in html
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


# ── Nacional ambíguo (dois fornecedores) — fail-closed ──────────────────

def test_nacional_ambíguo_com_dois_fornecedores_vira_unknown(db, make_product):
    from loja.modules.models import Product, Supplier, SupplierProduct
    from loja.services.shipping_quote import classify_line

    # Produto nacional com dois fornecedores manuais ativos, sem variante
    product = Product(id=uuid.uuid4(), name="Nacional Ambig", price=Decimal("20.00"), stock=5, is_active=True)
    db.session.add(product)
    db.session.flush()
    for suffix in ("A", "B"):
        supplier = Supplier(id=uuid.uuid4(), name=f"Supp {suffix}", channel_type="manual")
        db.session.add(supplier)
        db.session.flush()
        db.session.add(SupplierProduct(id=uuid.uuid4(), supplier_id=supplier.id, product_id=product.id, supplier_product_id=f"SP_{suffix}", cost=Decimal("8.00"), currency="BRL", is_active=True))
    db.session.commit()
    channel, key = classify_line(product.id, None)
    assert channel == "unknown"
    assert key is None


def test_calcular_frete_ambíguo_bloqueia_e_nao_grava_confirmacao(client, db, make_product, make_user, monkeypatch):
    # Produto nacional ambíguo: /calcular_frete deve bloquear e não deixar confirmação utilizável
    from loja.modules.models import Product, Supplier, SupplierProduct

    user = make_user()
    _login(client, user)
    product = Product(id=uuid.uuid4(), name="Ambig", price=Decimal("20.00"), stock=5, is_active=True)
    db.session.add(product)
    db.session.flush()
    for suffix in ("A", "B"):
        supplier = Supplier(id=uuid.uuid4(), name=f"Supp {suffix}", channel_type="manual")
        db.session.add(supplier)
        db.session.flush()
        db.session.add(SupplierProduct(id=uuid.uuid4(), supplier_id=supplier.id, product_id=product.id, supplier_product_id=f"SP_{suffix}", cost=Decimal("8.00"), currency="BRL", is_active=True))
    db.session.commit()
    monkeypatch.setitem(client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", True)
    monkeypatch.setitem(client.application.config, "SHIPPING_ORIGIN_CEP", ORIGIN)
    monkeypatch.setitem(client.application.config, "SHIPPING_DOMESTIC_RATES", {("013", "013"): (Decimal("5.00"), 1, 2)})
    # Limpa qualquer cotação anterior
    with client.session_transaction() as sess:
        sess.pop("shipping_quote", None)
    resp = client.post("/calcular_frete", json={"cep": "01310100", "product_id": str(product.id), "quantity": 1})
    assert resp.status_code == 409
    assert "indisponível" in resp.get_json()["error"]
    with client.session_transaction() as sess:
        # Nenhuma confirmação utilizável deve ter sido gravada
        assert sess.get("shipping_quote") is None or sess["shipping_quote"].get("cep") != "01310100"


def test_checkout_ambíguo_nao_cria_pedido_nem_reserva(client, db, make_user, monkeypatch):
    import loja.views.payments_routes as pr
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.modules.models import Product, Supplier, SupplierProduct

    user = make_user()
    product = Product(id=uuid.uuid4(), name="Ambig2", price=Decimal("20.00"), stock=5, is_active=True)
    db.session.add(product)
    db.session.flush()
    for suffix in ("A", "B"):
        supplier = Supplier(id=uuid.uuid4(), name=f"Supp {suffix}", channel_type="manual")
        db.session.add(supplier)
        db.session.flush()
        db.session.add(SupplierProduct(id=uuid.uuid4(), supplier_id=supplier.id, product_id=product.id, supplier_product_id=f"SP_{suffix}", cost=Decimal("8.00"), currency="BRL", is_active=True))
    db.session.commit()
    _login(client, user)
    monkeypatch.setitem(client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", True)
    monkeypatch.setitem(client.application.config, "SHIPPING_ORIGIN_CEP", ORIGIN)
    monkeypatch.setitem(client.application.config, "SHIPPING_DOMESTIC_RATES", {("013", "013"): (Decimal("5.00"), 1, 2)})
    chamadas = []
    monkeypatch.setattr(pr, "create_preference", lambda order: chamadas.append(order) or {"preference_id": "pref", "checkout_url": "https://mp.example"})
    antes_stock = product.stock
    antes_orders = Order.query.count()
    resp = client.post("/pagamentos/mercado-pago", data={
        "checkout_type": "product", "product_id": str(product.id), "quantity": 1, "selected_size": "M",
        "nome": "Cliente Teste", "email": user.email, "cep": "01310100", "estado": "SP",
        "endereco": "Rua Teste", "numero": "10", "bairro": "Centro", "cidade": "São Paulo",
    }, follow_redirects=True)
    assert "indisponível" in resp.get_data(as_text=True).lower()
    assert Order.query.count() == antes_orders
    assert product.stock == antes_stock
    assert chamadas == []
    # Garante que tarifa de estoque próprio não foi usada como fallback
    with client.session_transaction() as sess:
        assert sess.get("shipping_quote") is None


def test_tarifa_estoque_proprio_nao_fallback_para_ambíguo(db, make_product, monkeypatch):
    from loja.modules.models import Product, Supplier, SupplierProduct
    from loja.services.shipping_quote import classify_line

    # Produto ambíguo não deve cair em estoque próprio
    product = Product(id=uuid.uuid4(), name="Ambig3", price=Decimal("20.00"), stock=5, is_active=True)
    db.session.add(product)
    db.session.flush()
    for suffix in ("A", "B"):
        supplier = Supplier(id=uuid.uuid4(), name=f"Supp {suffix}", channel_type="manual")
        db.session.add(supplier)
        db.session.flush()
        db.session.add(SupplierProduct(id=uuid.uuid4(), supplier_id=supplier.id, product_id=product.id, supplier_product_id=f"SP_{suffix}", cost=Decimal("8.00"), currency="BRL", is_active=True))
    db.session.commit()
    channel, key = classify_line(product.id, None)
    assert channel == "unknown"
    # Produto realmente próprio, sem vínculo, usa "own"
    proprio = Product(id=uuid.uuid4(), name="Proprio", price=Decimal("20.00"), stock=5, is_active=True)
    db.session.add(proprio)
    db.session.commit()
    channel2, key2 = classify_line(proprio.id, None)
    assert channel2 == "domestic" and key2 is None


def test_variante_vinculada_escolhe_origem_correta(client, db, monkeypatch):
    from loja.modules.models import Product, ProductVariant, Supplier, SupplierProduct
    from loja.services.shipping_quote import classify_line, resolve_group_origin

    # Produto com dois fornecedores; cria duas variantes, cada uma vinculada
    # a um fornecedor diferente (cenário inequívoco por variante).
    product = Product(id=uuid.uuid4(), name="VarAmbig", price=Decimal("20.00"), stock=5, is_active=True)
    db.session.add(product)
    db.session.flush()
    suppliers = []
    supplier_products = []
    for suffix in ("A", "B"):
        supplier = Supplier(id=uuid.uuid4(), name=f"Supp {suffix}", channel_type="manual")
        db.session.add(supplier)
        db.session.flush()
        suppliers.append(supplier)
        sp = SupplierProduct(id=uuid.uuid4(), supplier_id=supplier.id, product_id=product.id, supplier_product_id=f"SP_{suffix}", cost=Decimal("8.00"), currency="BRL", is_active=True)
        db.session.add(sp)
        db.session.flush()
        supplier_products.append(sp)
    variants = []
    for sp, sku in ((supplier_products[0], "SKU_A"), (supplier_products[1], "SKU_B")):
        variant = ProductVariant(id=uuid.uuid4(), product_id=product.id, supplier_product_id=sp.id, supplier_sku_id=sku, stock=5, is_active=True)
        db.session.add(variant)
        db.session.flush()
        variants.append(variant)
    db.session.commit()

    monkeypatch.setitem(client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", True)
    monkeypatch.setitem(
        client.application.config, "SHIPPING_DOMESTIC_ORIGINS",
        {
            str(suppliers[0].id): "01310100",
            str(suppliers[1].id): "77100000",
        },
    )
    monkeypatch.setitem(
        client.application.config, "SHIPPING_DOMESTIC_RATES",
        {("013", "771"): (Decimal("7.50"), 3, 5),
         ("771", "771"): (Decimal("9.00"), 2, 4)},
    )
    # SHIPPING_ORIGIN_CEP existe só para estoque próprio; nunca é fallback
    # de fornecedor ambíguo.
    monkeypatch.setitem(client.application.config, "SHIPPING_ORIGIN_CEP", "99999999")

    # Variante A vincula à origem de A; variante B, à de B.
    channel, key = classify_line(product.id, variants[0].id)
    assert channel == "domestic" and key == suppliers[0].id
    assert resolve_group_origin(channel, key) == "01310100"
    channel, key = classify_line(product.id, variants[1].id)
    assert channel == "domestic" and key == suppliers[1].id
    assert resolve_group_origin(channel, key) == "77100000"

    # Vínculo da variante INATIVO: não determina origem; reavalia os ativos
    # do produto. Como ainda há dois fornecedores manuais ativos sem variante
    # inequívoca, o produto é ambíguo (fail-closed), nunca "own".
    variants[0].is_active = False
    db.session.commit()
    channel, key = classify_line(product.id, variants[0].id)
    assert channel == "unknown" and key is None
    assert resolve_group_origin(channel, key) is None

    # Even com uma variante ativa apontando para supplier B, variante B resolve
    # normalmente (vínculo ativo + fornecedor válido).
    variants[1].is_active = True
    db.session.commit()
    channel, key = classify_line(product.id, variants[1].id)
    assert channel == "domestic" and key == suppliers[1].id
    assert resolve_group_origin(channel, key) == "77100000"


def test_produto_proprio_usa_own_e_origin_cep(client, db, make_user, monkeypatch):
    from loja.services.shipping_quote import classify_line

    user = make_user()
    _login(client, user)
    # Produto sem vínculo com fornecedor: estoque próprio
    from loja.modules.models import Product
    proprio = Product(id=uuid.uuid4(), name="Proprio2", price=Decimal("20.00"), stock=5, is_active=True)
    db.session.add(proprio)
    db.session.commit()
    channel, key = classify_line(proprio.id, None)
    assert channel == "domestic" and key is None
    # Com SHIPPING_ORIGIN_CEP configurado, deve cotar
    monkeypatch.setitem(client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", True)
    monkeypatch.setitem(client.application.config, "SHIPPING_ORIGIN_CEP", ORIGIN)
    monkeypatch.setitem(client.application.config, "SHIPPING_DOMESTIC_RATES", {("013", "013"): (Decimal("5.00"), 1, 2)})
    resp = client.post("/calcular_frete", json={"cep": "01310100", "product_id": str(proprio.id), "quantity": 1})
    assert resp.status_code == 200
    assert resp.get_json()["valor"] == "5.00"


def test_cotacao_anterior_nao_reutilizada_apos_ambiguidade(client, db, make_user, make_product, monkeypatch):
    import loja.views.payments_routes as pr
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.modules.models import Product, Supplier, SupplierProduct

    user = make_user()
    product = Product(id=uuid.uuid4(), name="Muda", price=Decimal("20.00"), stock=5, is_active=True)
    db.session.add(product)
    db.session.flush()
    supplier = Supplier(id=uuid.uuid4(), name="Supp Unico", channel_type="manual")
    db.session.add(supplier)
    db.session.flush()
    db.session.add(SupplierProduct(id=uuid.uuid4(), supplier_id=supplier.id, product_id=product.id, supplier_product_id="SP_U", cost=Decimal("8.00"), currency="BRL", is_active=True))
    db.session.commit()
    _login(client, user)
    from loja.modules.models import Supplier as _Supplier

    supplier_id = None
    with client.application.test_request_context():
        from loja.services.shipping_quote import classify_line as _cl

        _, supplier_id = _cl(product.id, None)
    monkeypatch.setitem(client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", True)
    # Origem por fornecedor (não SHIPPING_ORIGIN_CEP, que é só para estoque próprio)
    monkeypatch.setitem(
        client.application.config, "SHIPPING_DOMESTIC_ORIGINS",
        {str(supplier_id): ORIGIN},
    )
    monkeypatch.setitem(client.application.config, "SHIPPING_DOMESTIC_RATES", {("013", "013"): (Decimal("5.00"), 1, 2)})
    # Cota com um fornecedor (válida) e guarda na sessão
    resp = client.post("/calcular_frete", json={"cep": "01310100", "product_id": str(product.id), "quantity": 1})
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        assert sess["shipping_quote"]["cep"] == "01310100"
    # Agora adiciona segundo fornecedor, tornando o produto ambíguo
    supplier2 = Supplier(id=uuid.uuid4(), name="Supp 2", channel_type="manual")
    db.session.add(supplier2)
    db.session.flush()
    db.session.add(SupplierProduct(id=uuid.uuid4(), supplier_id=supplier2.id, product_id=product.id, supplier_product_id="SP_2", cost=Decimal("8.00"), currency="BRL", is_active=True))
    db.session.commit()
    chamadas = []
    monkeypatch.setattr(pr, "create_preference", lambda order: chamadas.append(order) or {"preference_id": "pref", "checkout_url": "https://mp.example"})
    antes = Order.query.count()
    resp = client.post("/pagamentos/mercado-pago", data={
        "checkout_type": "product", "product_id": str(product.id), "quantity": 1, "selected_size": "M",
        "nome": "Cliente Teste", "email": user.email, "cep": "01310100", "estado": "SP",
        "endereco": "Rua Teste", "numero": "10", "bairro": "Centro", "cidade": "São Paulo",
    }, follow_redirects=True)
    assert "indisponível" in resp.get_data(as_text=True).lower()
    assert Order.query.count() == antes
    assert chamadas == []


# ── Flag desligada (default): tarifa comercial temporária única ──────────

def test_flag_desligada_calcula_frete_10(client, db, make_user, monkeypatch):
    user = make_user()
    _login(client, user)
    _manual_supplier_cart(db, user)
    monkeypatch.setitem(client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", False)
    resp = client.post("/calcular_frete", json={"cart": True, "cep": "77100000"})
    assert resp.status_code == 200
    assert resp.get_json()["valor"] == "10.00"
    # Sem confirmação agrupada: checkout flat não exige sessão.
    with client.session_transaction() as sess:
        sess.pop("shipping_quote", None)


def test_flag_desligada_subtotal_acima_249_paga_10(client, db, make_user, monkeypatch):
    # Não há frete grátis automático: a tarifa temporária é única.
    user = make_user()
    _login(client, user)
    _manual_supplier_cart(db, user)
    monkeypatch.setitem(client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", False)
    from loja.modules.models import Cart, CartItem
    cart = Cart.query.filter_by(user_id=user.id).first()
    for item in CartItem.query.filter_by(cart_id=cart.id).all():
        item.unit_price = Decimal("200.00")
    db.session.commit()
    resp = client.post("/calcular_frete", json={"cart": True, "cep": "77100000"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["subtotal"] == "400.00"
    assert data["valor"] == "10.00"
    assert data["total"] == "410.00"


def test_flag_desligada_quantidade_nao_altera_tarifa(client, db, make_user, make_product, monkeypatch):
    # Quantidade altera subtotal, não a tarifa comercial temporária.
    user = make_user()
    product = make_product(stock=10)
    _login(client, user)
    monkeypatch.setitem(client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", False)
    resp = client.post(
        "/calcular_frete",
        json={"cep": "01310100", "product_id": str(product.id), "quantity": 5},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["quantity"] == 5
    assert data["valor"] == "10.00"
    assert data["total"] == f"{float(data['subtotal']) + 10:.2f}"


def test_flag_desligada_checkout_10_consistente(client, db, make_user, monkeypatch):
    import loja.views.payments_routes as pr
    from loja.modules.getway_mercado_pago.payment_models import Order

    # /calcular_frete, Order.shipping, Order.total e a preferência Mercado
    # Pago usam exatamente o mesmo valor (R$ 10,00).
    user = make_user()
    _login(client, user)
    _manual_supplier_cart(db, user)
    monkeypatch.setitem(client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", False)

    resp_calc = client.post("/calcular_frete", json={"cart": True, "cep": "77100000"})
    assert resp_calc.status_code == 200
    calc = resp_calc.get_json()
    assert calc["valor"] == "10.00"

    preferencias = []
    monkeypatch.setattr(pr, "create_preference", lambda order: preferencias.append(order) or {
        "preference_id": "pref-flat", "checkout_url": "https://mp.example/flat",
    })
    antes = Order.query.count()
    resp = client.post(
        "/pagamentos/mercado-pago",
        data=_cart_checkout_form(user, "77100000"),
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert Order.query.count() == antes + 1
    order = Order.query.filter_by(user_id=user.id).one()
    assert order.shipping == Decimal("10.00")
    assert order.total == Decimal(calc["total"])
    assert len(preferencias) == 1
    # A preferência Mercado Pago cobra o total oficial (subtotal + frete 10,00).
    assert preferencias[0].total == order.total


# ── Validação estrita da tarifa temporária ────────────────────────────────

def test_temporary_flat_rate_10_005_arredonda_para_10_01(client, db, make_user, monkeypatch):
    import loja.views.payments_routes as pr
    from loja.modules.getway_mercado_pago.payment_models import Order

    # "10.005" deve arredondar para 10.01 (ROUND_HALF_UP em duas casas).
    user = make_user()
    _login(client, user)
    _manual_supplier_cart(db, user)
    monkeypatch.setitem(client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", False)
    monkeypatch.setitem(client.application.config, "SHIPPING_TEMPORARY_FLAT_RATE", "10.005")

    resp_calc = client.post("/calcular_frete", json={"cart": True, "cep": "77100000"})
    assert resp_calc.status_code == 200
    calc = resp_calc.get_json()
    assert calc["valor"] == "10.01"
    # Nunca vira frete zero.
    assert calc["valor"] != "0.00"

    preferencias = []
    monkeypatch.setattr(pr, "create_preference", lambda order: preferencias.append(order) or {
        "preference_id": "pref-round", "checkout_url": "https://mp.example/round",
    })
    antes = Order.query.count()
    resp = client.post(
        "/pagamentos/mercado-pago",
        data=_cart_checkout_form(user, "77100000"),
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert Order.query.count() == antes + 1
    order = Order.query.filter_by(user_id=user.id).one()
    assert order.shipping == Decimal("10.01")
    assert order.total == Decimal(calc["total"])
    assert len(preferencias) == 1
    assert preferencias[0].total == order.total
    # Sanitizado: a resposta não ecoa o valor bruto "10.005".
    assert "10.005" not in resp_calc.get_data(as_text=True)


@pytest.mark.parametrize(
    "raw",
    [
        "abc",
        "-10",
        "-0.01",
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        "",
        "   ",
        "9999.999",
        "10000.00",
        "1e999999",
    ],
)
def test_temporary_flat_rate_config_invalida_bloqueia(client, db, make_user, monkeypatch, raw):
    import loja.views.payments_routes as pr
    from loja.modules.getway_mercado_pago.payment_models import Order
    from loja.modules.models import Product

    user = make_user()
    _login(client, user)
    cart, _ = _manual_supplier_cart(db, user)
    monkeypatch.setitem(client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", False)
    monkeypatch.setitem(client.application.config, "SHIPPING_TEMPORARY_FLAT_RATE", raw)

    # /calcular_frete falha controlada, não 200 e nunca frete zero.
    resp_calc = client.post("/calcular_frete", json={"cart": True, "cep": "77100000"})
    assert resp_calc.status_code in {400, 409}
    body = resp_calc.get_json() or {}
    assert "error" in body
    assert body.get("valor") is None
    # Sanitizado: resposta de erro não deve ecoar o valor bruto quando
    # ele é distintivo ("abc"). Para "-10"/"nan"/"infinity" o eco
    # sanitizado também não expõe o valor, mas checar substring no HTML
    # inteiro gera falso positivo (CSS contém "-10", etc).
    if str(raw).lower() == "abc":
        assert "abc" not in resp_calc.get_data(as_text=True).lower()
    # Não cria pedido, não reserva estoque, não chama MP.
    chamadas = []
    monkeypatch.setattr(pr, "create_preference", lambda order: chamadas.append(order) or {
        "preference_id": "pref-bad", "checkout_url": "https://mp.example/bad",
    })
    from loja.modules.models import CartItem

    product = Product.query.first()
    estoque_antes = product.stock if product else None
    itens_carrinho_antes = CartItem.query.filter_by(cart_id=cart.id).count()
    antes = Order.query.count()
    resp = client.post(
        "/pagamentos/mercado-pago",
        data=_cart_checkout_form(user, "77100000"),
        follow_redirects=True,
    )
    assert Order.query.count() == antes
    assert chamadas == []
    assert CartItem.query.filter_by(cart_id=cart.id).count() == itens_carrinho_antes
    if product is not None and estoque_antes is not None:
        db.session.refresh(product)
        assert product.stock == estoque_antes
    # A página completa contém UUIDs e outros dados aleatórios; portanto
    # não procure o token curto "abc" no HTML inteiro. Prove a sanitização
    # pela mensagem pública estável, sem nome da variável, traceback ou
    # exceção decimal.
    page = resp.get_data(as_text=True).lower()
    assert "frete desatualizado" in page
    assert "shipping_temporary_flat_rate" not in page
    assert "invalidoperation" not in page
    assert "traceback" not in page
    # Bloqueio implica não ter virado frete zero implícito (pedido não existe).


def test_temporary_flat_rate_api_invalida_direta(app, monkeypatch):
    from loja.services.shipping_quote import QuoteUnavailable, temporary_flat_rate

    for raw in [
        "abc", "-10", "-0.01", "NaN", "sNaN", "Infinity", "-Infinity",
        "", "   ", "9999.999", "10000.00", "1e999999",
    ]:
        monkeypatch.setitem(app.config, "SHIPPING_TEMPORARY_FLAT_RATE", raw)
        with app.app_context():
            try:
                temporary_flat_rate()
            except QuoteUnavailable as exc:
                assert exc.code == "invalid_quote"
            else:
                assert False, f"raw={raw!r} deveria falhar"

    # "10.005" válido e arredondado.
    monkeypatch.setitem(app.config, "SHIPPING_TEMPORARY_FLAT_RATE", "10.005")
    with app.app_context():
        assert temporary_flat_rate() == Decimal("10.01")


def test_temporary_flat_rate_limites_e_default(app, monkeypatch):
    from loja.services.shipping_quote import (
        SHIPPING_TEMPORARY_FLAT_RATE_CFG,
        SHIPPING_TEMPORARY_FLAT_RATE_DEFAULT,
        SHIPPING_TEMPORARY_FLAT_RATE_MAX,
        temporary_flat_rate,
    )

    # Variável ausente usa apenas o default comercial explícito.
    monkeypatch.delitem(app.config, SHIPPING_TEMPORARY_FLAT_RATE_CFG, raising=False)
    with app.app_context():
        assert temporary_flat_rate() == SHIPPING_TEMPORARY_FLAT_RATE_DEFAULT
        assert temporary_flat_rate() == Decimal("10.00")

    # O valor exatamente no teto operacional é representável e válido.
    monkeypatch.setitem(
        app.config,
        SHIPPING_TEMPORARY_FLAT_RATE_CFG,
        str(SHIPPING_TEMPORARY_FLAT_RATE_MAX),
    )
    with app.app_context():
        value = temporary_flat_rate()
        assert value == Decimal("9999.99")
        assert value.as_tuple().exponent == -2


def test_temporary_flat_rate_no_limite_consistente_ponta_a_ponta(
    client, db, make_user, monkeypatch
):
    import loja.views.payments_routes as pr
    from loja.modules.getway_mercado_pago.payment_models import Order

    user = make_user()
    _login(client, user)
    _manual_supplier_cart(db, user)
    monkeypatch.setitem(
        client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", False
    )
    monkeypatch.setitem(
        client.application.config, "SHIPPING_TEMPORARY_FLAT_RATE", "9999.99"
    )

    calculation = client.post(
        "/calcular_frete", json={"cart": True, "cep": "77100000"}
    )
    assert calculation.status_code == 200
    calculated = calculation.get_json()
    assert calculated["valor"] == "9999.99"

    preferences = []
    monkeypatch.setattr(
        pr,
        "create_preference",
        lambda order: preferences.append(order) or {
            "preference_id": "pref-flat-limit",
            "checkout_url": "https://mp.example/flat-limit",
        },
    )
    response = client.post(
        "/pagamentos/mercado-pago",
        data=_cart_checkout_form(user, "77100000"),
        follow_redirects=False,
    )
    assert response.status_code == 302
    order = Order.query.filter_by(user_id=user.id).one()
    assert order.shipping == Decimal("9999.99")
    assert order.total == Decimal(calculated["total"])
    assert len(preferences) == 1
    assert preferences[0].total == order.total


@pytest.mark.parametrize("raw", ["10000.00", "1e999999"])
def test_temporary_flat_rate_invalida_preserva_variante_cupom_e_gateway(
    client, db, make_user, make_coupon, monkeypatch, raw
):
    import loja.views.payments_routes as pr
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
    from loja.modules.models import Product, ProductVariant

    user = make_user()
    product, variant = _supplier_link(db)
    with db.session.no_autoflush:
        stored_product = db.session.get(Product, product.id)
        stored_product.is_draft = False
        stored_product.pricing_status = "priced"
        stored_product.is_active = True
        db.session.commit()

    coupon = make_coupon(
        code="FRETE-LIMITE",
        discount_type="fixed",
        discount_value="1.00",
        usage_limit=10,
    )
    _login(client, user)
    monkeypatch.setitem(
        client.application.config, "SHIPPING_GROUPED_QUOTE_ENABLED", False
    )
    monkeypatch.setitem(
        client.application.config, "SHIPPING_TEMPORARY_FLAT_RATE", raw
    )

    gateway_calls = []
    monkeypatch.setattr(
        pr,
        "create_preference",
        lambda order: gateway_calls.append(order) or {
            "preference_id": "must-not-exist",
            "checkout_url": "https://mp.example/must-not-exist",
        },
    )

    product_stock_before = db.session.get(Product, product.id).stock
    variant_stock_before = db.session.get(ProductVariant, variant.id).stock
    coupon_uses_before = coupon.times_used
    orders_before = Order.query.count()
    items_before = OrderItem.query.count()

    response = client.post(
        "/pagamentos/mercado-pago",
        data={
            "checkout_type": "product",
            "product_id": str(product.id),
            "variant_id": str(variant.id),
            "quantity": 1,
            "selected_size": "M",
            "coupon_code": coupon.code,
            "nome": "Cliente Teste",
            "email": user.email,
            "telefone": "11999999999",
            "cep": "01310100",
            "estado": "SP",
            "endereco": "Rua Teste",
            "numero": "10",
            "bairro": "Centro",
            "cidade": "São Paulo",
        },
        follow_redirects=True,
    )

    page = response.get_data(as_text=True).lower()
    assert "frete desatualizado" in page
    assert "invalidoperation" not in page
    assert "traceback" not in page
    assert gateway_calls == []
    assert Order.query.count() == orders_before
    assert OrderItem.query.count() == items_before
    assert db.session.get(Product, product.id).stock == product_stock_before
    assert db.session.get(ProductVariant, variant.id).stock == variant_stock_before
    db.session.refresh(coupon)
    assert coupon.times_used == coupon_uses_before
