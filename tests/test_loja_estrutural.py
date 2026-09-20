"""Revisão estrutural da loja (sem fatos comerciais presumidos).

Cobre apenas comportamento verificável em código:
- rodapé sem destinos href="#" (redes/ajuda/trocas/rastreio/privacidade/termos/cookies);
- rotas institucionais reais (/condicoes-de-uso, /politica-privacidade, /contato,
  /sobre, /acompanhar-pedido/) respondem 200; /termos segue 404;
- meios de pagamento do rodapé refletem o checkout (Mercado Pago: bandeiras de
  cartão; sem PayPal);
- CTA "Ver coleção" do Sobre leva à listagem (/produtos);
- home sem promoções: sem cronômetro (data-end) e sem título de urgência,
  com mensagem neutra; com promoção: cronômetro presente.
"""

import re
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from flask import render_template


def _get(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, f"GET {path} retornou {resp.status_code}"
    return resp.get_data(as_text=True)


def _produto_transiente(nome="Camiseta Teste"):
    return SimpleNamespace(
        id=str(uuid.uuid4()),
        name=nome,
        price=Decimal("59.90"),
        image1="https://exemplo.com/img.png",
    )


def _promo_transiente(produto, ends_at=None):
    return SimpleNamespace(
        product=produto,
        sale_price=Decimal("39.90"),
        is_favorited=False,
        ends_at=ends_at,
    )


def _render_home(app, promotions):
    """Renderiza a home com contexto explícito, sem ler nem escrever Promotion no banco."""
    with app.test_request_context("/"):
        return render_template(
            "main/index.html",
            hero_slides=[],
            categories=[],
            banner={},
            promotions=promotions,
            products=[],
            testimonials=[],
            store_policy={},
            structured_data=None,
        )


def test_footer_sem_href_placeholder(client, db):
    html = _get(client, "/")
    assert 'href="#"' not in html


def test_footer_rastrear_pedido_aponta_para_acompanhamento(client, db):
    html = _get(client, "/")
    assert "/acompanhar-pedido" in html


def test_footer_legal_aponta_para_rotas_reais(client, db):
    html = _get(client, "/")
    assert "/politica-privacidade" in html
    assert "/condicoes-de-uso" in html
    assert "Cookies" not in html


def test_footer_sem_paypal_com_bandeiras_de_cartao(client, db):
    html = _get(client, "/")
    assert "paypal" not in html.lower()
    for brand in ("Visa", "Mastercard", "Amex"):
        assert brand in html


def test_rotas_institucionais_respondem_200(client, db):
    for path in (
        "/sobre",
        "/politica-privacidade",
        "/condicoes-de-uso",
        "/contato",
        "/acompanhar-pedido/",
        "/produtos",
    ):
        _get(client, path)


def test_termos_curto_continua_404(client, db):
    resp = client.get("/termos")
    assert resp.status_code == 404


# ── Links do rodapé cadastrados pelo admin ──────────────────────────────

import loja.cache as _cache_mod


@pytest.fixture
def _footer_fresco():
    """Evita cache entre testes sem tocar em registros alheios."""
    _cache_mod._cache.pop("footer_data", None)
    yield
    _cache_mod._cache.pop("footer_data", None)


@pytest.fixture
def _links_proprios(db):
    """Guarda ids criados pelo teste para limpar só o que é nosso."""
    criados = []
    yield criados
    from loja.modules.models import (
        CategoryLink,
        LegalLink,
        NavLink,
        SocialLink,
    )

    for modelo in (SocialLink, NavLink, CategoryLink, LegalLink):
        for ident in [c[0] for c in criados if c[1] is modelo]:
            obj = db.session.get(modelo, ident)
            if obj is not None:
                db.session.delete(obj)
    db.session.commit()


def _criar(modelo, db, criados, **kwargs):
    obj = modelo(id=uuid.uuid4(), **kwargs)
    db.session.add(obj)
    db.session.commit()
    criados.append((obj.id, modelo))
    return obj


def test_rodape_omite_sociais_invalidas_mostra_validas(
    client, db, _footer_fresco, _links_proprios
):
    from loja.modules.models import SocialLink

    _criar(
        SocialLink, db, _links_proprios,
        label="RedeValida", url="https://instagram.com/pollomodas",
    )
    _criar(
        SocialLink, db, _links_proprios, label="RedeRuim", url="#",
    )
    html = _get(client, "/")
    assert "https://instagram.com/pollomodas" in html
    assert "RedeRuim" not in html
    assert 'href="#"' not in html


def test_rodape_omite_nav_javascript_mostra_interno(
    client, db, _footer_fresco, _links_proprios
):
    from loja.modules.models import NavLink

    _criar(
        NavLink, db, _links_proprios, label="NavValida", url="/sobre",
    )
    _criar(
        NavLink, db, _links_proprios,
        label="NavRuim", url="javascript:alert(1)",
    )
    html = _get(client, "/")
    assert "NavValida" in html
    assert "NavRuim" not in html
    assert "javascript:" not in html


def test_rodape_omite_categoria_placeholder(client, db, _footer_fresco, _links_proprios):
    from loja.modules.models import CategoryLink

    _criar(
        CategoryLink, db, _links_proprios,
        label="CatValida", url="/produtos", slug="cat-valida",
        image="https://exemplo.com/cat.png",
    )
    _criar(
        CategoryLink, db, _links_proprios,
        label="CatRuim", url="#", slug="cat-ruim",
        image="https://exemplo.com/cat.png",
    )
    html = _get(client, "/")
    assert "CatValida" in html
    assert "CatRuim" not in html
    assert 'href="#"' not in html


def test_rodape_omite_legal_data_mostra_rota(client, db, _footer_fresco, _links_proprios):
    from loja.modules.models import LegalLink

    _criar(
        LegalLink, db, _links_proprios,
        label="LegalValido", url="/politica-privacidade",
    )
    _criar(
        LegalLink, db, _links_proprios,
        label="LegalRuim", url="data:text/html,<h1>x</h1>",
    )
    html = _get(client, "/")
    assert "LegalValido" in html
    assert "LegalRuim" not in html
    assert "data:text/html" not in html


def _post_admin(client, admin_user, path, data):
    client.post(
        "/login",
        data={"email": admin_user.email, "password": "senha123"},
        follow_redirects=True,
    )
    return client.post(path, data=data, follow_redirects=False)


@pytest.mark.parametrize(
    "path,data",
    [
        ("/admin/add_social_link", {"label": "X", "url": "#"}),
        ("/admin/add_nav_link", {"label": "X", "url": ""}),
        (
            "/admin/add_category_link",
            {"label": "X", "url": "javascript:alert(1)", "slug": "x", "image": "https://exemplo.com/x.png"},
        ),
        ("/admin/add_legal_link", {"label": "X", "url": "http://exemplo.com/x"}),
    ],
)
def test_admin_recusa_placeholder_novo(client, db, admin_user, path, data):
    from loja.modules.models import (
        CategoryLink,
        LegalLink,
        NavLink,
        SocialLink,
    )

    modelos = {
        "/admin/add_social_link": SocialLink,
        "/admin/add_nav_link": NavLink,
        "/admin/add_category_link": CategoryLink,
        "/admin/add_legal_link": LegalLink,
    }
    antes = modelos[path].query.count()
    resp = _post_admin(client, admin_user, path, data)
    assert resp.status_code == 200
    assert "URL inválida" in resp.get_data(as_text=True)
    assert modelos[path].query.count() == antes


def test_sobre_cta_leva_a_listagem(client, db):
    html = _get(client, "/sobre")
    match = re.search(
        r'<a href="([^"]+)" class="sobre-cta-btn">\s*Ver coleção',
        html,
    )
    assert match, "CTA 'Ver coleção' não encontrado no Sobre."
    assert match.group(1) == "/produtos"


def test_home_sem_promocao_secao_neutra(app):
    html = _render_home(app, [])
    assert "data-end=" not in html
    assert "dealsCountdown" not in html
    assert "Acaba em" not in html
    assert "deals-title" not in html
    assert "Termina em" not in html
    assert "chegam já já" in html


def test_home_promocao_sem_prazo_visivel_sem_relogio(app):
    html = _render_home(app, [_promo_transiente(_produto_transiente(), ends_at=None)])
    assert "Camiseta Teste" in html
    assert "deals-title" in html
    assert "Termina em" not in html
    assert "data-end=" not in html
    assert "dealsCountdown" not in html
    assert "Acaba em" not in html


def test_home_promocao_com_prazo_mostra_prazo_correto(app):
    fim = datetime.now().replace(microsecond=0) + timedelta(days=2, hours=3)
    html = _render_home(
        app, [_promo_transiente(_produto_transiente(), ends_at=fim)]
    )
    esperado = fim.strftime("%d/%m às %H:%M")
    assert f"Termina em {esperado}" in html
    assert "data-end=" not in html
    assert "dealsCountdown" not in html


def test_home_promos_mistas_um_prazo_por_card(app):
    com_prazo = _promo_transiente(
        _produto_transiente("Com Prazo"),
        ends_at=datetime.now().replace(microsecond=0) + timedelta(days=2),
    )
    sem_prazo = _promo_transiente(_produto_transiente("Sem Prazo"), ends_at=None)
    html = _render_home(app, [com_prazo, sem_prazo])
    assert html.count("Termina em") == 1
    assert "Com Prazo" in html and "Sem Prazo" in html
    assert "data-end=" not in html
    assert "Acaba em" not in html
