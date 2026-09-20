"""FASE 9.0.0 — captura sanitizada de origem de tráfego no pedido.

Cobre o serviço de sanitização first-touch e a persistência da origem no Order
durante o checkout. Falha fechado: origem inválida/ausente vira NULL, nunca
payload bruto.
"""
import uuid
from decimal import Decimal

from loja.modules.getway_mercado_pago.payment_models import Order
from loja.services.acquisition_source import (
    acquisition_to_order_kwargs,
    capture_acquisition,
    order_acquisition,
    sanitize_referrer,
    sanitize_utm,
)


class _FakeRequest:
    def __init__(self, args=None, referrer=None, host="pollomodas.com.br"):
        self.args = args or {}
        self.referrer = referrer
        self.host = host


# ---------------------------------------------------------------------------
# Sanitização UTM
# ---------------------------------------------------------------------------

def test_utm_clean_values_preserved():
    req = _FakeRequest({
        "utm_source": "instagram",
        "utm_medium": "cpc",
        "utm_campaign": "lancamento-verao_2026",
    })
    out = sanitize_utm(req)
    assert out == {
        "source": "instagram",
        "medium": "cpc",
        "campaign": "lancamento-verao_2026",
    }


def test_utm_strips_disallowed_chars():
    req = _FakeRequest({
        "utm_source": "ig<script>alert(1)</script>",
        "utm_medium": "cpc%0Aattaque",
        "utm_campaign": "cam <img src=x>",
    })
    out = sanitize_utm(req)
    assert out["source"] == "igscriptalert1script"
    assert out["medium"] == "cpc0Aattaque"
    assert out["campaign"] == "cam img srcx"
    assert all(
        char.isalnum() or char in "._- " for value in out.values() if value
        for char in value
    )


def test_utm_empty_or_missing_becomes_none():
    req = _FakeRequest({})
    out = sanitize_utm(req)
    assert out == {"source": None, "medium": None, "campaign": None}

    req = _FakeRequest({"utm_source": "", "utm_medium": "  ", "utm_campaign": None})
    out = sanitize_utm(req)
    assert out == {"source": None, "medium": None, "campaign": None}


def test_utm_truncated_to_max_length():
    req = _FakeRequest({"utm_source": "a" * 300})
    out = sanitize_utm(req)
    assert len(out["source"]) == 64


# ---------------------------------------------------------------------------
# Sanitização de referrer
# ---------------------------------------------------------------------------

def test_referrer_external_host_only():
    req = _FakeRequest(referrer="https://instagram.com/p/abc?utm_source=x y")
    assert sanitize_referrer(req) == "instagram.com"


def test_referrer_preserves_subdomain():
    req = _FakeRequest(referrer="https://www.google.com/search?q=vestido")
    assert sanitize_referrer(req) == "google.com"


def test_referrer_internal_is_dropped():
    req = _FakeRequest(
        referrer="https://pollomodas.com.br/produtos",
        host="pollomodas.com.br",
    )
    assert sanitize_referrer(req) is None


def test_referrer_www_self_is_dropped():
    req = _FakeRequest(
        referrer="https://www.pollomodas.com.br/",
        host="www.pollomodas.com.br",
    )
    assert sanitize_referrer(req) is None


def test_referrer_empty_or_invalid():
    assert sanitize_referrer(_FakeRequest(referrer="")) is None
    assert sanitize_referrer(_FakeRequest(referrer="  ")) is None
    assert sanitize_referrer(_FakeRequest(referrer="http://")) is None


# ---------------------------------------------------------------------------
# First-touch na sessão
# ---------------------------------------------------------------------------

def test_capture_stores_first_valid_origin():
    req = _FakeRequest({
        "utm_source": "instagram",
        "utm_medium": "cpc",
        "utm_campaign": "verao",
    })
    session = {}
    out = capture_acquisition(req, session=session)

    assert out["source"] == "instagram"
    assert out["medium"] == "cpc"
    assert out["campaign"] == "verao"
    assert session["acquisition"] == {"source": "instagram", "medium": "cpc",
                                      "campaign": "verao"}


def test_capture_does_not_overwrite_first_touch():
    session = {
        "acquisition": {
            "source": "google",
            "medium": "organic",
            "campaign": None,
            "referrer": None,
        }
    }
    req = _FakeRequest({"utm_source": "instagram"})
    out = capture_acquisition(req, session=session)

    assert out["source"] == "google"
    assert session["acquisition"]["source"] == "google"


def test_capture_referrer_when_no_utm():
    req = _FakeRequest(
        args={},
        referrer="https://tiktok.com/@pollo?utm_ignored=1",
    )
    session = {}
    out = capture_acquisition(req, session=session)

    assert out["source"] is None
    assert out["referrer"] == "tiktok.com"
    assert session["acquisition"]["referrer"] == "tiktok.com"


def test_capture_internal_referrer_alone_does_not_persist():
    req = _FakeRequest(
        args={},
        referrer="https://pollomodas.com.br/produtos/abc",
        host="pollomodas.com.br",
    )
    session = {}
    out = capture_acquisition(req, session=session)

    assert out == {"source": None, "medium": None,
                   "campaign": None, "referrer": None}
    assert "acquisition" not in session


def test_order_acquisition_reads_session():
    session = {
        "acquisition": {
            "source": "google",
            "medium": "cpc",
            "campaign": "shop",
            "referrer": None,
        }
    }
    out = order_acquisition(session=session)
    assert out["source"] == "google"
    assert out["campaign"] == "shop"


def test_acquisition_to_order_kwargs():
    kwargs = acquisition_to_order_kwargs(
        {"source": "ig", "medium": "cpc", "campaign": "v"}
    )
    assert kwargs == {
        "acquisition_source": "ig",
        "acquisition_medium": "cpc",
        "acquisition_campaign": "v",
        "acquisition_referrer": None,
    }


# ---------------------------------------------------------------------------
# Integração: checkout persiste origem no Order
# ---------------------------------------------------------------------------

def _checkout_order(db, product, *, quantity=1):
    from loja.modules.getway_mercado_pago.payment_models import OrderItem

    unit_price = Decimal(str(product.price))
    subtotal = unit_price * quantity
    shipping = Decimal("18.90")
    order = Order(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        checkout_type="product",
        status="pending_payment",
        subtotal=subtotal,
        shipping=shipping,
        discount=Decimal("0.00"),
        total=subtotal + shipping,
        stock_reserved=True,
        customer_name="Cliente Teste",
        customer_email="cliente@example.com",
        zip_code="01310100",
        street="Rua Teste",
        number="10",
        neighborhood="Centro",
        city="São Paulo",
        state="SP",
        acquisition_source="instagram",
        acquisition_medium="cpc",
        acquisition_campaign="verao",
        acquisition_referrer=None,
    )
    order.items.append(
        OrderItem(
            id=uuid.uuid4(),
            product_id=product.id,
            product_name=product.name,
            selected_size="M",
            quantity=quantity,
            unit_price=unit_price,
            line_total=subtotal,
        )
    )
    db.session.add(order)
    db.session.commit()
    return order


def test_order_columns_persist_acquisition(db, make_product):
    product = make_product(stock=5)
    order = _checkout_order(db, product)

    assert order.acquisition_source == "instagram"
    assert order.acquisition_medium == "cpc"
    assert order.acquisition_campaign == "verao"
    assert order.acquisition_referrer is None


def test_order_model_defaults_nullable(db, make_product):
    product = make_product(stock=5)
    order = _checkout_order(db, product)
    order_id = order.id

    db.session.expire_all()
    stored = db.session.get(Order, order_id)
    assert stored.acquisition_source == "instagram"
    assert stored.acquisition_campaign == "verao"


def test_checkout_persists_captured_first_touch_origin(
    db, client, make_user, make_product, monkeypatch
):
    import loja.views.payments_routes as pr

    user = make_user()
    product = make_product(stock=5)

    client.post("/login", data={"email": user.email, "password": "senha123"})

    monkeypatch.setattr(pr, "quote_checkout_shipping", lambda item_data, cep: Decimal("18.90"))
    monkeypatch.setattr(
        pr,
        "create_preference",
        lambda order: {
            "preference_id": "pref-9000",
            "checkout_url": "https://mp.example/x",
        },
    )

    # Primeira página da sessão traz UTM de campanha.
    with client.session_transaction() as sess:
        sess["acquisition"] = {
            "source": "tiktok",
            "medium": "cpc",
            "campaign": "verao-2026",
            "referrer": None,
        }

    resp = client.post(
        "/pagamentos/mercado-pago",
        data={
            "checkout_type": "product",
            "product_id": str(product.id),
            "quantity": "1",
            "selected_size": "M",
            "nome": "Cliente Teste",
            "email": user.email,
            "cep": "01310100",
            "estado": "SP",
            "endereco": "Rua Teste",
            "numero": "10",
            "bairro": "Centro",
            "cidade": "São Paulo",
        },
    )
    assert resp.status_code == 302

    order = Order.query.filter_by(user_id=user.id).first()
    assert order is not None
    assert order.acquisition_source == "tiktok"
    assert order.acquisition_medium == "cpc"
    assert order.acquisition_campaign == "verao-2026"
    assert order.acquisition_referrer is None


def test_checkout_without_origin_keeps_null(
    db, client, make_user, make_product, monkeypatch
):
    import loja.views.payments_routes as pr

    user = make_user()
    product = make_product(stock=5)

    client.post("/login", data={"email": user.email, "password": "senha123"})

    monkeypatch.setattr(pr, "quote_checkout_shipping", lambda item_data, cep: Decimal("18.90"))
    monkeypatch.setattr(
        pr,
        "create_preference",
        lambda order: {
            "preference_id": "pref-9001",
            "checkout_url": "https://mp.example/x",
        },
    )

    resp = client.post(
        "/pagamentos/mercado-pago",
        data={
            "checkout_type": "product",
            "product_id": str(product.id),
            "quantity": "1",
            "selected_size": "M",
            "nome": "Cliente Teste",
            "email": user.email,
            "cep": "01310100",
            "estado": "SP",
            "endereco": "Rua Teste",
            "numero": "10",
            "bairro": "Centro",
            "cidade": "São Paulo",
        },
    )
    assert resp.status_code == 302

    order = Order.query.filter_by(user_id=user.id).first()
    assert order is not None
    assert order.acquisition_source is None
    assert order.acquisition_medium is None
    assert order.acquisition_campaign is None
    assert order.acquisition_referrer is None