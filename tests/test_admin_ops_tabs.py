"""Pedidos, Revisão manual, Prontidão e Recuperação como abas de /admin.

Operação vive no dashboard (`#orders`, `#manual-review`, `#sales-readiness`,
`#checkout-recovery`) com carregamento sob demanda via `/admin/painel/*`.
URLs antigas de listagem redirecionam para a aba (preservando parâmetros
reconhecidos e não pessoais); POSTs mantêm contrato/CSRF e voltam para a aba.
Comportamentos de fragmento/histórico vivem no JS — sem executor JS neste
ambiente, a fiação é verificada estaticamente e a limitação está registrada.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from loja.modules.getway_mercado_pago.payment_models import Order

TABS = {
    "orders": "/admin/painel/orders",
    "manual-review": "/admin/painel/manual-review",
    "sales-readiness": "/admin/painel/sales-readiness",
    "checkout-recovery": "/admin/painel/checkout-recovery",
}

NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def _order(db, user, status="pending_payment", **kw):
    order = Order(
        id=uuid.uuid4(), user_id=user.id, checkout_type="product", status=status,
        subtotal=Decimal("58"), shipping=Decimal("10"), discount=Decimal("0"),
        total=Decimal("68"), customer_name="Cliente de teste",
        customer_email=user.email, zip_code="77066520", street="Rua Teste",
        number="1", neighborhood="Centro", city="Palmas", state="TO",
        **kw,
    )
    db.session.add(order)
    db.session.commit()
    return order


@pytest.fixture(autouse=True)
def _clean_ops_tabs_data(db):
    """Isolamento: apaga pedidos e singletons criados em cada teste.

    O dashboard cria singletons no primeiro acesso e os testes criam
    pedidos — sem limpeza, vazam para outros arquivos (contagens de
    liberação, render condicional de add_site_config).
    """
    from loja.modules.getway_mercado_pago.payment_models import (
        Order, OrderItem, Payment,
    )
    from loja.modules.models import ContactInfo, FooterConfig, SiteConfig

    order_ids = {row[0] for row in db.session.query(Order.id).all()}
    singleton_ids = {
        model: {row[0] for row in db.session.query(model.id).all()}
        for model in (SiteConfig, FooterConfig, ContactInfo)
    }
    yield
    new_orders = [
        row[0] for row in db.session.query(Order.id).all()
        if row[0] not in order_ids
    ]
    if new_orders:
        OrderItem.query.filter(
            OrderItem.order_id.in_(new_orders)
        ).delete(synchronize_session=False)
        Payment.query.filter(
            Payment.order_id.in_(new_orders)
        ).delete(synchronize_session=False)
        Order.query.filter(Order.id.in_(new_orders)).delete(
            synchronize_session=False
        )
    for model, known in singleton_ids.items():
        for row in model.query.all():
            if row.id not in known:
                db.session.delete(row)
    db.session.commit()


@pytest.fixture()
def admin_client(client, admin_user, login):
    login(admin_user.email)
    return client


def _dashboard_html(admin_client):
    resp = admin_client.get("/admin")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


# --------------------------------------------------------------------------
# 1) Abas registradas no dashboard (desktop + mobile) com lazy containers
# --------------------------------------------------------------------------

def test_dashboard_registra_quatro_abas_operacao(admin_client):
    html = _dashboard_html(admin_client)
    for tab, partial in TABS.items():
        assert html.count(f'data-panel="{tab}"') >= 2, tab  # desktop + mobile
        assert f'id="panel-{tab}"' in html, tab
        assert f'data-partial-container="{tab}"' in html, tab
        assert f'data-partial-src="{partial}"' in html, tab


def test_nav_operacao_sem_links_para_paginas_antigas(admin_client):
    html = _dashboard_html(admin_client)
    for old in (
        "/admin/pedidos", "/admin/pedidos/revisao", "/admin/prontidao-vendas",
        "/admin/recuperacao-checkout",
    ):
        assert f'href="{old}"' not in html, old


# --------------------------------------------------------------------------
# 2) Parciais: conteúdo, métodos, permissão
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tab,partial", list(TABS.items()))
def test_parcial_entrega_conteudo(admin_client, db, admin_user, tab, partial):
    _order(db, admin_user)
    resp = admin_client.get(partial)
    assert resp.status_code == 200
    assert f'data-partial-root="{tab}"' in resp.get_data(as_text=True)


def test_parcial_pedidos_filtra_e_pagina(admin_client, db, admin_user):
    pending = _order(db, admin_user, status="pending_payment")
    paid = _order(db, admin_user, status="paid")
    html = admin_client.get(
        "/admin/painel/orders", query_string={"status": "paid"}
    ).get_data(as_text=True)
    assert paid.order_number in html
    assert pending.order_number not in html
    assert 'data-partial-form' in html
    assert "/admin/painel/orders" in html  # paginação/filtro ficam na aba


def test_parcial_rejeita_post(admin_client):
    for partial in TABS.values():
        assert admin_client.post(partial).status_code == 405


@pytest.mark.parametrize("tab,partial", list(TABS.items()))
def test_parcial_exige_admin(client, login, make_user, tab, partial):
    assert client.get(partial).status_code == 302  # anônimo
    login(make_user(is_admin=False).email)
    assert client.get(partial).status_code == 302  # não admin


def test_parciais_sem_script_e_sem_ids(admin_client):
    seen = set()
    for partial in TABS.values():
        html = admin_client.get(partial).get_data(as_text=True)
        assert "<script" not in html.lower(), partial
        for found in re.findall(r'id="([^"]+)"', html):
            assert found not in seen, f"id duplicado: {found}"
            seen.add(found)


def test_parcial_status_invalido_falha_fechado(admin_client):
    assert admin_client.get(
        "/admin/painel/orders", query_string={"status": "invalido"}
    ).status_code == 400


# --------------------------------------------------------------------------
# 3) URLs antigas encaminham para as abas (parâmetros válidos preservados)
# --------------------------------------------------------------------------

def test_urls_antigas_redirecionam_para_abas(admin_client):
    cases = [
        ("/admin/pedidos", "/admin#orders"),
        ("/admin/pedidos/revisao", "/admin#manual-review"),
        ("/admin/prontidao-vendas", "/admin#sales-readiness"),
        ("/admin/recuperacao-checkout", "/admin#checkout-recovery"),
    ]
    for old, new in cases:
        resp = admin_client.get(old)
        assert resp.status_code == 302, old
        assert resp.headers["Location"] == new, old


def test_redirect_pedidos_preserva_estado_pagina_e_descarta_resto(admin_client):
    resp = admin_client.get(
        "/admin/pedidos",
        query_string={"status": "paid", "page": "2", "q": "X", "email": "a@b.c"},
    )
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert location.startswith("/admin#orders?"), location
    assert "status=paid" in location and "page=2" in location
    assert "email=" not in location and "q=" not in location


def test_urls_antigas_exigem_admin(client):
    for old in (
        "/admin/pedidos", "/admin/pedidos/revisao", "/admin/prontidao-vendas",
        "/admin/recuperacao-checkout",
    ):
        assert client.get(old).status_code == 302


# --------------------------------------------------------------------------
# 4) Ações voltam para a aba correspondente
# --------------------------------------------------------------------------

def test_aprovar_rejeitar_voltam_para_aba(admin_client, db, admin_user, make_product):
    from tests.test_manual_review_exit import _manual_order

    product = make_product(stock=10)
    order = _manual_order(db, admin_user, product)
    resp = admin_client.post(
        f"/admin/pedidos/revisao/{order.id}/aprovar",
        data={"confirm_approve": "1"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/admin#manual-review"

    order2 = _manual_order(db, admin_user, product)
    resp = admin_client.post(
        f"/admin/pedidos/revisao/{order2.id}/rejeitar",
        data={"confirm_reject": "1", "reason": "motivo"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/admin#manual-review"


def test_recuperacao_volta_para_aba(admin_client, db, admin_user):
    order = _order(db, admin_user)
    for path, data in [
        (f"/admin/recuperacao-checkout/{order.id}/enviar", {}),
        (f"/admin/recuperacao-checkout/{order.id}/ignorar", {"confirm_skip": "1"}),
        (f"/admin/reservas-expiradas/{order.id}/liberar", {"confirm_release": "1"}),
    ]:
        resp = admin_client.post(path, data=data)
        assert resp.status_code == 302, path
        assert resp.headers["Location"] == "/admin#checkout-recovery", path


def test_lote_recuperacao_volta_para_aba(admin_client, db, admin_user):
    _order(db, admin_user)
    resp = admin_client.post("/admin/recuperacao-checkout", data={})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/admin#checkout-recovery"


# --------------------------------------------------------------------------
# 5) Abrir abas não executa ações operacionais nem consultas externas
# --------------------------------------------------------------------------

def test_abrir_abas_sem_efeitos_colaterais(
    admin_client, db, admin_user, monkeypatch
):
    import loja.modules.enviar_email as mail_mod
    import loja.services.supplier_freight_quotes as quotes_mod

    calls = []

    def _forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("aba não pode executar ação operacional")

    monkeypatch.setattr(mail_mod, "enviar_email", _forbidden)
    monkeypatch.setattr(
        quotes_mod, "request_reference_freight_quote", _forbidden, raising=False
    )
    order = _order(
        db, admin_user,
        mp_preference_expires_at=NOW - timedelta(hours=1),
    )
    stock_before = list(
        db.session.query(Order.id, Order.status, Order.stock_reserved).all()
    )
    for partial in TABS.values():
        assert admin_client.get(partial).status_code == 200
    assert calls == []
    assert list(
        db.session.query(Order.id, Order.status, Order.stock_reserved).all()
    ) == stock_before
    assert db.session.get(Order, order.id).status == "pending_payment"


def test_formularios_das_parciais_trazem_csrf(admin_client, db, admin_user):
    _order(db, admin_user, status="manual_review")
    _order(
        db, admin_user, status="pending_payment", stock_reserved=True,
        mp_preference_expires_at=NOW - timedelta(hours=1),
    )
    for partial in (
        "/admin/painel/manual-review", "/admin/painel/checkout-recovery",
    ):
        assert 'name="csrf_token"' in admin_client.get(partial).get_data(as_text=True)


# --------------------------------------------------------------------------
# 6) Fiação do fragmento/histórico (estática: sem executor JS no ambiente)
# --------------------------------------------------------------------------

def test_js_preserva_selecao_via_historico():
    src = open(
        "public/static/scripts/dashboard_admin.js", encoding="utf-8"
    ).read()
    assert "history.pushState" in src
    assert "popstate" in src and "hashchange" in src
    assert "partialSrc" in src and "data-partial-retry" in src
    assert "Tentar novamente" in src
    # Cliques empurram histórico (replaceState só na carga/popstate).
    assert src.count("history.pushState") >= 1
