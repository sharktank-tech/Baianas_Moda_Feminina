"""Prontidão para primeira venda real (somente leitura, fail-closed).

Relógio congelado: nenhuma data depende da execução real. Configurações
via monkeypatch (restauração automática). Sem compra, sem provider, sem
mutação.
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

# Relógio congelado (UTC-3 o ano todo em São Paulo, sem DST).
FROZEN_NOW = datetime(2030, 6, 15, 10, 0)  # naive UTC

POLICY_VARS = (
    "UNIT_ECONOMICS_GATEWAY_FEE_PERCENT",
    "UNIT_ECONOMICS_INSTALLMENT_COST_PERCENT",
    "UNIT_ECONOMICS_TAX_PERCENT",
    "UNIT_ECONOMICS_ACQUISITION_COST_PER_ORDER",
    "UNIT_ECONOMICS_REFUND_RESERVE_PERCENT",
)


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    # O serviço importa utcnow de loja.utils dentro da função.
    monkeypatch.setattr("loja.utils.utcnow", lambda: FROZEN_NOW)
    return FROZEN_NOW


@pytest.fixture()
def full_policy(monkeypatch):
    for var in POLICY_VARS:
        monkeypatch.setenv(var, "0")


@pytest.fixture(autouse=True)
def _cleanup_session(db):
    yield
    db.session.rollback()
    db.session.remove()


def _product(db, **overrides):
    from loja.modules.models import Product

    params = {
        "id": uuid.uuid4(),
        "name": "Produto Pronto",
        "description": "Descrição completa do produto",
        "price": Decimal("49.90"),
        "image1": "https://exemplo.com/img.png",
        "stock": 10,
        "is_active": True,
        "is_draft": False,
        "source": "internal",
        "currency": "BRL",
        "target_margin_percent": Decimal("30.00"),
        "pricing_status": "priced",
    }
    params.update(overrides)
    product = Product(**params)
    db.session.add(product)
    db.session.flush()
    return product


def _supplier(db, *, channel="manual", seller_id=None, **overrides):
    from loja.modules.models import Supplier

    params = {
        "id": uuid.uuid4(),
        "name": "Fornecedor Teste",
        "channel_type": channel,
        "seller_id": seller_id,
    }
    params.update(overrides)
    supplier = Supplier(**params)
    db.session.add(supplier)
    db.session.flush()
    return supplier


def _link(db, product, supplier, **overrides):
    from loja.modules.models import SupplierProduct

    params = {
        "id": uuid.uuid4(),
        "supplier_id": supplier.id,
        "product_id": product.id,
        "supplier_product_id": "SP-CANONICO-1",
        "cost": Decimal("20.00"),
        "currency": "BRL",
        "freight_status": "available",
        "shipping_cost": Decimal("5.00"),
        "landed_cost": Decimal("25.00"),
        "freight_currency": "BRL",
        "is_active": True,
    }
    params.update(overrides)
    link = SupplierProduct(**params)
    db.session.add(link)
    db.session.flush()
    return link


def _variant(db, product, link=None, *, stock=3, active=True):
    from loja.modules.models import ProductVariant

    variant = ProductVariant(
        id=uuid.uuid4(),
        product_id=product.id,
        color="Rosa",
        size="M",
        stock=stock,
        is_active=active,
        supplier_product_id=link.id if link is not None else None,
    )
    db.session.add(variant)
    db.session.flush()
    return variant


REF_CEP = "01310100"


def _fresh(
    link_id,
    *,
    minutes_ago=10,
    currency="BRL",
    cost="5.00",
    dest_prefix="013",
    option=None,
    qty=1,
    ttl_seconds=1800,
    quoted_at=None,
    drop_currency=False,
):
    entry = {
        "cost": cost,
        "dest_prefix": dest_prefix,
        "option": option,
        "qty": qty,
        "currency": currency,
        "quoted_at": (
            quoted_at
            if quoted_at is not None
            else FROZEN_NOW - timedelta(minutes=minutes_ago)
        ),
        "ttl_seconds": ttl_seconds,
    }
    if drop_currency:
        del entry["currency"]
    return {str(link_id): entry}


def _evaluate(product, **kwargs):
    from loja.services.sales_readiness import evaluate_product_readiness

    params = {
        "now": FROZEN_NOW,
        "charged_freight": Decimal("10.00"),
        "ref_dest_cep": REF_CEP,
        "ref_option": None,
    }
    params.update(kwargs)
    return evaluate_product_readiness(product, **params)


# ── Serviço: estados ────────────────────────────────────────────────────

def test_complete_product_is_pronto(db, full_policy):
    from loja.services import sales_readiness as sr

    product = _product(db, source="aliexpress")
    supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
    link = _link(db, product, supplier)

    result = _evaluate(product, fresh_quotes=_fresh(link.id))

    assert result.state == sr.READY
    assert result.reasons == ("manual_fulfillment_required",)
    assert result.proven_cost_display == "R$ 25,00"
    assert result.proven_logistic_display == "R$ 5,00"
    assert result.charged_freight_display == "R$ 10,00"
    assert result.origin == "AliExpress"


def test_national_single_supplier_can_be_pronto(db, full_policy):
    from loja.services import sales_readiness as sr

    product = _product(db, source="internal")
    supplier = _supplier(db, channel="manual")
    link = _link(db, product, supplier)

    result = _evaluate(product, fresh_quotes=_fresh(link.id))

    assert result.state == sr.READY
    assert result.origin == "Nacional"


@pytest.mark.parametrize(
    "override,code",
    [
        ({"is_active": False}, "product_inactive"),
        ({"is_draft": True}, "product_draft"),
        ({"pricing_status": "ready"}, "pricing_not_ready"),
        ({"price": Decimal("0.00")}, "missing_sale_price"),
        ({"description": None}, "missing_description"),
        ({"stock": 0}, "no_stock"),
    ],
)
def test_product_blocks(db, full_policy, override, code):
    from loja.services import sales_readiness as sr

    # source internal: o guarda de banco impede draft/não-priced em
    # AliExpress; as checagens de produto independem da origem.
    product = _product(db, source="internal", **override)
    supplier = _supplier(db, channel="manual")
    link = _link(db, product, supplier)

    result = _evaluate(product, fresh_quotes=_fresh(link.id))

    assert result.state == sr.BLOCKED
    assert code in result.reasons


def test_missing_image_is_revisao_not_block(db, full_policy):
    from loja.services import sales_readiness as sr

    product = _product(db, source="aliexpress", image1="")
    supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
    link = _link(db, product, supplier)

    result = _evaluate(product, fresh_quotes=_fresh(link.id))

    assert result.state == sr.REVIEW
    assert "missing_image" in result.reasons


def test_aliexpress_without_link_is_blocked(db, full_policy):
    from loja.services import sales_readiness as sr

    product = _product(db, source="aliexpress")

    result = _evaluate(product, fresh_quotes={})

    assert result.state == sr.BLOCKED
    assert "missing_supplier" in result.reasons


def test_aliexpress_ambiguous_links_blocked(db, full_policy):
    from loja.services import sales_readiness as sr

    product = _product(db, source="aliexpress")
    supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
    _link(db, product, supplier, supplier_product_id="SP-A")
    _link(db, product, supplier, supplier_product_id="SP-B")

    result = _evaluate(product, fresh_quotes={})

    assert result.state == sr.BLOCKED
    assert "ambiguous_supplier" in result.reasons


def test_national_ambiguous_suppliers_blocked(db, full_policy):
    from loja.services import sales_readiness as sr

    product = _product(db, source="internal")
    _link(db, product, _supplier(db, name="Sup A"), supplier_product_id="SP-A")
    _link(db, product, _supplier(db, name="Sup B"), supplier_product_id="SP-B")

    result = _evaluate(product, fresh_quotes={})

    assert result.state == sr.BLOCKED
    assert "ambiguous_supplier" in result.reasons


def test_missing_sku_blocked(db, full_policy):
    from loja.services import sales_readiness as sr

    product = _product(db, source="aliexpress")
    supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
    link = _link(db, product, supplier, supplier_product_id=None)

    result = _evaluate(product, fresh_quotes=_fresh(link.id))

    assert result.state == sr.BLOCKED
    assert "missing_sku" in result.reasons


def test_missing_cost_blocked(db, full_policy):
    from loja.services import sales_readiness as sr

    product = _product(db, source="aliexpress")
    supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
    link = _link(db, product, supplier, cost=None, landed_cost=None)

    result = _evaluate(product, fresh_quotes=_fresh(link.id))

    assert result.state == sr.BLOCKED
    assert "missing_cost" in result.reasons


def test_non_brl_currency_blocked(db, full_policy):
    from loja.services import sales_readiness as sr

    product = _product(db, source="aliexpress", currency="USD")
    supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
    link = _link(db, product, supplier, currency="USD", freight_currency="USD")

    result = _evaluate(product, fresh_quotes=_fresh(link.id))

    assert result.state == sr.BLOCKED
    assert "currency_mismatch" in result.reasons


def test_expired_quote_blocked(db, full_policy):
    from loja.services import sales_readiness as sr

    product = _product(db, source="aliexpress")
    supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
    link = _link(db, product, supplier)

    result = _evaluate(product, fresh_quotes=_fresh(link.id, minutes_ago=120))

    assert result.state == sr.BLOCKED
    assert "shipping_quote_expired" in result.reasons


def test_unproven_logistic_never_pronto(db, full_policy):
    """R$ 10 cobrados não são prova: sem fresh, no máximo REVISÃO."""
    from loja.services import sales_readiness as sr

    product = _product(db, source="aliexpress")
    supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
    _link(db, product, supplier)

    result = _evaluate(product, fresh_quotes={})

    assert result.state == sr.REVIEW
    assert "shipping_cost_unproven" in result.reasons
    assert result.charged_freight_display == "R$ 10,00"
    assert result.proven_logistic_display == "—"


def test_negative_margin_blocked(db, full_policy):
    from loja.services import sales_readiness as sr

    product = _product(db, source="aliexpress", price=Decimal("20.00"))
    supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
    link = _link(db, product, supplier)

    result = _evaluate(product, fresh_quotes=_fresh(link.id))

    assert result.state == sr.BLOCKED
    assert "negative_margin" in result.reasons


def test_economics_incomplete_without_policy(db):
    """Sem política de custos configurada, economia não se prova."""
    from loja.services import sales_readiness as sr

    product = _product(db, source="aliexpress")
    supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
    link = _link(db, product, supplier)

    result = _evaluate(product, fresh_quotes=_fresh(link.id))

    assert result.state == sr.BLOCKED
    assert "economics_incomplete" in result.reasons


def test_own_stock_capped_at_revisao(db, app, full_policy, monkeypatch):
    from loja.services import sales_readiness as sr

    monkeypatch.setitem(app.config, "SHIPPING_ORIGIN_CEP", "01310100")
    product = _product(db, source="internal", cost=Decimal("20.00"))

    result = _evaluate(product, fresh_quotes={})

    assert result.state == sr.REVIEW
    assert result.origin == "Estoque próprio"
    assert "shipping_cost_unproven" in result.reasons


def test_own_stock_without_origin_blocked(db, app, full_policy, monkeypatch):
    from loja.services import sales_readiness as sr

    monkeypatch.setitem(app.config, "SHIPPING_ORIGIN_CEP", None)
    monkeypatch.setitem(app.config, "SHIPPING_DOMESTIC_ORIGINS", {})
    product = _product(db, source="internal", cost=Decimal("20.00"))

    result = _evaluate(product, fresh_quotes={})

    assert result.state == sr.BLOCKED
    assert "missing_origin" in result.reasons


def test_invalid_variant_surfaced_not_hidden(db, full_policy):
    """Uma variante inválida aparece nos motivos; com vendável, REVISÃO."""
    from loja.services import sales_readiness as sr

    product = _product(db, source="aliexpress")
    supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
    link = _link(db, product, supplier)
    _variant(db, product, link, stock=3)
    _variant(db, product, link, stock=0)

    result = _evaluate(product, fresh_quotes=_fresh(link.id))

    assert result.state == sr.REVIEW
    assert "no_stock" in result.reasons  # da variante zerada, visível
    assert result.stock == 3


def test_all_variants_invalid_blocked(db, full_policy):
    from loja.services import sales_readiness as sr

    product = _product(db, source="aliexpress")
    supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
    link = _link(db, product, supplier)
    _variant(db, product, link, stock=0)
    _variant(db, product, link, stock=2, active=False)

    result = _evaluate(product, fresh_quotes=_fresh(link.id))

    assert result.state == sr.BLOCKED
    assert "no_stock" in result.reasons


def test_variant_bound_to_other_link_blocked(db, full_policy):
    from loja.services import sales_readiness as sr

    product = _product(db, source="aliexpress")
    supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
    link = _link(db, product, supplier)
    other = _link(
        db, product, supplier, supplier_product_id="SP-OUTRO", is_active=False
    )
    _variant(db, product, link, stock=3)
    _variant(db, product, other, stock=2)

    result = _evaluate(product, fresh_quotes=_fresh(link.id))

    assert result.state == sr.BLOCKED
    assert "missing_supplier" in result.reasons


def test_candidates_deterministic_order():
    from loja.services.sales_readiness import (
        ProductReadiness,
        summarize_readiness,
    )

    def _row(name, margin, stock, pid):
        return ProductReadiness(
            product_id=pid,
            name=name,
            origin="Nacional",
            supplier_name="Sup",
            price_display="R$ 49,90",
            proven_cost_display="R$ 25,00",
            charged_freight_display="R$ 10,00",
            proven_logistic_display="R$ 5,00",
            margin_display="—",
            stock=stock,
            state="PRONTO",
            reasons=("manual_fulfillment_required",),
            margin_sort=Decimal(margin),
        )

    rows = [
        _row("F", "10.00", 100, uuid.UUID(int=6)),
        _row("A", "50.00", 5, uuid.UUID(int=1)),
        _row("B", "50.00", 9, uuid.UUID(int=2)),
        _row("C", "50.00", 9, uuid.UUID(int=3)),
        _row("D", "40.00", 50, uuid.UUID(int=4)),
        _row("E", "30.00", 60, uuid.UUID(int=5)),
    ]
    first = [r.name for r in summarize_readiness(rows).candidates]
    second = [r.name for r in summarize_readiness(list(reversed(rows))).candidates]

    assert first == ["B", "C", "A", "D", "E"]
    assert second == first


def test_summarize_empty():
    from loja.services.sales_readiness import summarize_readiness

    summary = summarize_readiness([])

    assert (summary.pronto, summary.revisao, summary.bloqueado) == (0, 0, 0)
    assert summary.evaluated == 0
    assert summary.candidates == ()


# ── Rota/admin ──────────────────────────────────────────────────────────

def _snapshot(db):
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
    from loja.modules.models import Product, Supplier, SupplierProduct

    return {
        "products": Product.query.count(),
        "suppliers": Supplier.query.count(),
        "links": SupplierProduct.query.count(),
        "orders": Order.query.count(),
        "items": OrderItem.query.count(),
        "prices": sorted(str(p.price) for p in Product.query.all()),
        "stocks": sorted(int(p.stock or 0) for p in Product.query.all()),
    }


def test_route_requires_admin(client, regular_user, login, db, make_product):
    make_product()
    login(regular_user.email, "senha123")

    response = client.get("/admin/prontidao-vendas")

    assert response.status_code in (302, 303, 403)


def test_route_anonymous_redirect(client, db, make_product):
    make_product()

    response = client.get("/admin/prontidao-vendas")

    assert response.status_code in (302, 303)


def test_route_get_no_commit_no_mutation(
    client, db, admin_user, login, make_product, full_policy
):
    from loja.services import sales_readiness as sr

    product = make_product()
    login(admin_user.email, "senha123")
    before = _snapshot(db)

    response = client.get("/admin/prontidao-vendas")

    assert response.status_code == 200
    db.session.expire_all()
    assert _snapshot(db) == before
    assert b"Prontid\xc3\xa3o para vendas" in response.data


def test_route_get_no_external_boundaries(
    client, db, admin_user, login, make_product, monkeypatch
):
    import smtplib

    import loja.integrations.aliexpress.fulfillment as fulfillment
    import loja.modules.getway_mercado_pago.payment_service as payment_service

    make_product()
    login(admin_user.email, "senha123")
    calls = []
    def _recorder(name):
        def _record(*args, **kwargs):
            calls.append(name)

        return _record

    for module, name in [
        (payment_service, "create_preference"),
        (payment_service, "get_payment"),
        (payment_service, "get_sdk"),
        (fulfillment, "ensure_aliexpress_fulfillment"),
    ]:
        monkeypatch.setattr(module, name, _recorder(name))
    monkeypatch.setattr(
        smtplib, "SMTP", lambda *a, **k: calls.append("smtp.SMTP")
    )

    response = client.get("/admin/prontidao-vendas")

    assert response.status_code == 200
    assert calls == []


def test_route_no_mutation_forms_and_no_sensitive_data(
    client, db, admin_user, login, make_product, make_user, caplog
):
    user = make_user()
    product = make_product()
    supplier = _supplier(
        db,
        channel="aliexpress",
        seller_id="SELLER-SECRET-1",
        api_key="sk-secret-key",
        api_secret="ss-secret",
        access_token="tok-secret",
    )
    _link(db, product, supplier)
    login(admin_user.email, "senha123")

    with caplog.at_level("WARNING"):
        html = client.get("/admin/prontidao-vendas").get_data(as_text=True)

    assert "method=\"post\"" not in html.lower()
    assert product.name in html
    for secret in (
        "SELLER-SECRET-1",
        "sk-secret-key",
        "ss-secret",
        "tok-secret",
        user.email,
    ):
        assert secret not in html
        assert secret not in caplog.text


def test_route_counts_match_rows(client, db, admin_user, login, full_policy):  # noqa: ARG002
    import re

    from loja.services import sales_readiness as sr

    user_product = _product(db, source="aliexpress", name="Pronto Alpha")
    supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
    _link(db, user_product, supplier)
    # Sem fresh: item comprovado, só falta prova logística → REVISÃO.
    assert (
        sr.evaluate_product_readiness(
            user_product,
            fresh_quotes={},
            now=FROZEN_NOW,
            charged_freight=Decimal("10.00"),
        ).state
        == sr.REVIEW
    )
    login(admin_user.email, "senha123")
    html = client.get("/admin/prontidao-vendas").get_data(as_text=True)

    def _card(label):
        match = re.search(
            label + r"</div>\s*<div[^>]*>(\d+)<", html
        )
        assert match, label
        return int(match.group(1))

    pronto = _card("Produtos PRONTOS")
    revisao = _card("Em REVISÃO")
    bloqueado = _card("BLOQUEADOS")
    evaluated = _card("Produtos avaliados")
    assert pronto + revisao + bloqueado == evaluated
    assert evaluated >= 1
    assert "Pronto Alpha" in html
    assert ("Nenhum produto está comprovadamente pronto" in html) == (
        pronto == 0
    )


def test_route_empty_message_when_nothing_ready(
    client, db, admin_user, login, monkeypatch
):
    from loja.services import sales_readiness as sr

    calls = {"n": 0}

    def _only_blocked():
        from loja.services.sales_readiness import ProductReadiness

        calls["n"] += 1
        row = ProductReadiness(
            product_id=uuid.uuid4(),
            name="Travado",
            origin="Desconhecida",
            supplier_name="—",
            price_display="R$ 10,00",
            proven_cost_display="—",
            charged_freight_display="R$ 10,00",
            proven_logistic_display="—",
            margin_display="—",
            stock=0,
            state=sr.BLOCKED,
            reasons=("no_stock",),
        )
        return sr.summarize_readiness([row])

    monkeypatch.setattr(sr, "evaluate_sales_readiness", _only_blocked)
    login(admin_user.email, "senha123")

    html = client.get("/admin/prontidao-vendas").get_data(as_text=True)

    assert calls["n"] == 1
    assert "Nenhum produto está comprovadamente pronto para divulgação." in html


def test_recovery_expired_text_fixed(client, admin_user, login, db):
    login(admin_user.email, "senha123")

    html = client.get("/admin/recuperacao-checkout").get_data(as_text=True)

    assert "reserva ativa há mais de" not in html
    assert "expiração persistida já vencida" in html


# ── Frete fresco manda na economia; persistido divergente bloqueia ──────

def _stale_product(db, *, cost, shipping, landed, fresh_shipping):
    product = _product(db, source="aliexpress")
    supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
    link = _link(
        db,
        product,
        supplier,
        cost=Decimal(cost),
        shipping_cost=Decimal(shipping),
        landed_cost=Decimal(landed),
    )
    fresh = _fresh(link.id)
    fresh[str(link.id)]["cost"] = fresh_shipping
    return product, fresh


def test_stale_persisted_above_fresh_blocked(db, full_policy):
    """Persistido 15, fresco produz 25: diverge > 0,01 → BLOQUEADO."""
    from loja.services import sales_readiness as sr

    product, fresh = _stale_product(
        db, cost="10.00", shipping="5.00", landed="15.00", fresh_shipping="15.00"
    )

    result = _evaluate(product, fresh_quotes=fresh)

    assert result.state == sr.BLOCKED
    assert "landed_cost_stale" in result.reasons
    assert result.proven_cost_display == "R$ 25,00"
    assert result.proven_logistic_display == "R$ 15,00"


def test_stale_persisted_below_fresh_blocked(db, full_policy):
    """Persistido 35, fresco produz 25: diverge > 0,01 → BLOQUEADO."""
    from loja.services import sales_readiness as sr

    product, fresh = _stale_product(
        db, cost="20.00", shipping="15.00", landed="35.00", fresh_shipping="5.00"
    )

    result = _evaluate(product, fresh_quotes=fresh)

    assert result.state == sr.BLOCKED
    assert "landed_cost_stale" in result.reasons
    assert result.proven_cost_display == "R$ 25,00"


def test_margin_uses_fresh_current_landed(db, full_policy):
    """Persistido 25, fresco confirma 25: margem calculada sobre 25."""
    from loja.services import sales_readiness as sr

    product = _product(db, source="aliexpress", price=Decimal("49.90"))
    supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
    link = _link(
        db,
        product,
        supplier,
        cost=Decimal("20.00"),
        shipping_cost=Decimal("5.00"),
        landed_cost=Decimal("25.00"),
    )

    result = _evaluate(product, fresh_quotes=_fresh(link.id))

    assert result.state == sr.READY
    assert result.proven_cost_display == "R$ 25,00"
    assert result.margin_display == "49,90%"


def test_absent_fresh_explicitly_never_pronto(db, full_policy):
    from loja.services import sales_readiness as sr

    product = _product(db, source="internal", name="Sem Prova")
    supplier = _supplier(db, channel="manual")
    _link(db, product, supplier)

    result = _evaluate(product, fresh_quotes={})

    assert result.state != sr.READY
    assert result.state == sr.REVIEW


def test_invalid_fresh_config_fails_closed(app, db, full_policy, monkeypatch):
    from loja.services import sales_readiness as sr

    product = _product(db, source="aliexpress", name="Config Invalida")
    supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
    _link(db, product, supplier)
    monkeypatch.setitem(
        app.config, "SHIPPING_ALIEXPRESS_FRESH_QUOTES", ["nao-eh-dict"]
    )

    summary = sr.evaluate_sales_readiness(limit=500)
    mine = [r for r in summary.rows if r.product_id == product.id]

    assert len(mine) == 1
    assert mine[0].state != sr.READY


def test_fresh_snapshot_read_once_per_get(app, db, full_policy, monkeypatch):
    from collections import Counter

    from loja.services import sales_readiness as sr

    for suffix in ("Um", "Dois", "Tres"):
        product = _product(db, source="aliexpress", name=f"Snap {suffix}")
        supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
        _link(db, product, supplier)
    monkeypatch.setitem(app.config, "SHIPPING_ALIEXPRESS_FRESH_QUOTES", {})
    monkeypatch.setitem(app.config, "SALES_READINESS_DESTINATION_CEP", REF_CEP)

    calls = []
    original_get = app.config.get

    def counting_get(key, *args, **kwargs):
        calls.append(key)
        return original_get(key, *args, **kwargs)

    monkeypatch.setattr(app.config, "get", counting_get)
    sr.evaluate_sales_readiness(limit=50)

    tracked = Counter(
        key
        for key in calls
        if key
        in {
            "SHIPPING_ALIEXPRESS_FRESH_QUOTES",
            "SALES_READINESS_DESTINATION_CEP",
            "SALES_READINESS_SHIPPING_OPTION",
        }
    )
    # Cada configuração do cenário é lida exatamente uma vez por GET.
    assert tracked["SHIPPING_ALIEXPRESS_FRESH_QUOTES"] == 1
    assert tracked["SALES_READINESS_DESTINATION_CEP"] == 1
    assert tracked["SALES_READINESS_SHIPPING_OPTION"] == 1


def test_multi_link_variants_stay_blocked(db, full_policy):
    """Sem avaliação por variante nesta versão: múltiplos vínculos bloqueiam."""
    from loja.services import sales_readiness as sr

    product = _product(db, source="internal", name="Multi Vinculo")
    link_a = _link(db, product, _supplier(db, name="Sup A"), supplier_product_id="SP-A")
    link_b = _link(db, product, _supplier(db, name="Sup B"), supplier_product_id="SP-B")
    _variant(db, product, link_a, stock=3)
    _variant(db, product, link_b, stock=2)

    result = _evaluate(product, fresh_quotes={})

    assert result.state == sr.BLOCKED
    assert "ambiguous_supplier" in result.reasons


def _row_state(html, name):
    import re

    # Escopo restrito à tabela principal (após "Avaliação por produto"):
    # a tabela de candidatos repete nomes sem coluna de estado.
    main = html.split("Avaliação por produto", 1)[1]
    match = re.search(
        re.escape(name)
        + r"</td>(?:\s*<td[^>]*>.*?</td>){8}\s*<td[^>]*>(PRONTO|REVISÃO|BLOQUEADO)",
        main,
        re.DOTALL,
    )
    assert match, name
    return match.group(1)


def test_route_fresh_config_drives_pronto_then_regresses(
    client, db, admin_user, login, full_policy, monkeypatch
):
    import loja.integrations.aliexpress.fulfillment as fulfillment
    import loja.modules.getway_mercado_pago.payment_service as payment_service
    import smtplib

    product = _product(db, source="aliexpress", name="Rota Fresh Zeta")
    supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
    link = _link(db, product, supplier)
    monkeypatch.setitem(
        client.application.config, "SALES_READINESS_DESTINATION_CEP", REF_CEP
    )
    monkeypatch.setitem(
        client.application.config,
        "SHIPPING_ALIEXPRESS_FRESH_QUOTES",
        _fresh(link.id),
    )
    calls = []
    for module, name in [
        (payment_service, "create_preference"),
        (payment_service, "get_payment"),
        (payment_service, "get_sdk"),
        (fulfillment, "ensure_aliexpress_fulfillment"),
    ]:
        monkeypatch.setattr(module, name, lambda *a, **k: calls.append(True))
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: calls.append(True))
    login(admin_user.email, "senha123")

    html = client.get("/admin/prontidao-vendas").get_data(as_text=True)
    assert _row_state(html, "Rota Fresh Zeta") == "PRONTO"

    monkeypatch.delitem(
        client.application.config, "SHIPPING_ALIEXPRESS_FRESH_QUOTES", raising=False
    )
    html = client.get("/admin/prontidao-vendas").get_data(as_text=True)
    assert _row_state(html, "Rota Fresh Zeta") == "REVISÃO"

    assert calls == []


# ── Cenário de referência: prova só vale para CEP/opção/qty ─────────────

def _scenario_product(db, **overrides):
    product = _product(db, source="aliexpress", **overrides)
    supplier = _supplier(db, channel="aliexpress", seller_id="SELLER-1")
    link = _link(db, product, supplier)
    return product, link


def test_other_prefix_never_pronto(db, full_policy):
    from loja.services import sales_readiness as sr

    product, link = _scenario_product(db)

    result = _evaluate(
        product, fresh_quotes=_fresh(link.id, dest_prefix="771")
    )

    assert result.state != sr.READY
    assert result.state == sr.REVIEW


def test_qty_mismatch_never_pronto(db, full_policy):
    from loja.services import sales_readiness as sr

    product, link = _scenario_product(db)

    result = _evaluate(product, fresh_quotes=_fresh(link.id, qty=2))

    assert result.state != sr.READY
    assert result.state == sr.REVIEW


def test_option_mismatch_never_pronto(db, full_policy):
    from loja.services import sales_readiness as sr

    product, link = _scenario_product(db)

    result = _evaluate(
        product, fresh_quotes=_fresh(link.id, option="expressa")
    )

    assert result.state != sr.READY
    assert result.state == sr.REVIEW


def test_missing_currency_never_pronto(db, full_policy):
    """Moeda ausente falha fechado: nunca presumir BRL."""
    from loja.services import sales_readiness as sr

    product, link = _scenario_product(db)

    result = _evaluate(
        product, fresh_quotes=_fresh(link.id, drop_currency=True)
    )

    assert result.state != sr.READY
    assert result.state == sr.BLOCKED
    assert "currency_mismatch" in result.reasons


def test_explicit_brl_proceeds(db, full_policy):
    from loja.services import sales_readiness as sr

    product, link = _scenario_product(db)

    result = _evaluate(
        product, fresh_quotes=_fresh(link.id, currency="brl")
    )

    assert result.state == sr.READY


def test_no_reference_caps_at_revisao(db, full_policy):
    from loja.services import sales_readiness as sr

    product, link = _scenario_product(db)

    result = _evaluate(
        product, fresh_quotes=_fresh(link.id), ref_dest_cep=None
    )

    assert result.state == sr.REVIEW
    assert "shipping_destination_unproven" in result.reasons


def test_invalid_reference_cep_caps_at_revisao(db, full_policy):
    from loja.services import sales_readiness as sr

    product, link = _scenario_product(db)

    result = _evaluate(
        product, fresh_quotes=_fresh(link.id), ref_dest_cep="12"
    )

    assert result.state == sr.REVIEW
    assert "shipping_destination_unproven" in result.reasons


def test_future_quote_beyond_skew_never_pronto(db, full_policy):
    from loja.services import sales_readiness as sr

    product, link = _scenario_product(db)

    result = _evaluate(
        product,
        fresh_quotes=_fresh(
            link.id, quoted_at=FROZEN_NOW + timedelta(minutes=10)
        ),
    )

    assert result.state != sr.READY


def test_zero_ttl_rejected(db, full_policy):
    from loja.services import sales_readiness as sr

    product, link = _scenario_product(db)

    result = _evaluate(
        product, fresh_quotes=_fresh(link.id, ttl_seconds=0)
    )

    assert result.state != sr.READY


@pytest.mark.parametrize("cost", ["NaN", "Infinity"])
def test_nonfinite_cost_rejected(db, full_policy, cost):
    from loja.services import sales_readiness as sr

    product, link = _scenario_product(db)

    result = _evaluate(product, fresh_quotes=_fresh(link.id, cost=cost))

    assert result.state != sr.READY


def test_reference_status_labels(client, db, admin_user, login, monkeypatch):
    login(admin_user.email, "senha123")

    monkeypatch.setitem(
        client.application.config, "SALES_READINESS_DESTINATION_CEP", REF_CEP
    )
    html = client.get("/admin/prontidao-vendas").get_data(as_text=True)
    assert "Destino de referência configurado" in html
    assert "013•••••" in html
    assert "01310100" not in html  # CEP completo nunca exibido

    monkeypatch.delitem(
        client.application.config, "SALES_READINESS_DESTINATION_CEP", raising=False
    )
    html = client.get("/admin/prontidao-vendas").get_data(as_text=True)
    assert "Destino de referência pendente" in html
