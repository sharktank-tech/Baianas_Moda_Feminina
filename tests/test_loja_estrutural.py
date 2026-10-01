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


def _render_home(app, promotions, deals_do_dia=False):
    """Renderiza a home com contexto explícito, sem ler nem escrever Promotion no banco."""
    with app.test_request_context("/"):
        return render_template(
            "main/index.html",
            hero_slides=[],
            categories=[],
            banner={},
            promotions=promotions,
            deals_do_dia=deals_do_dia,
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


# ── Política comercial: troca em 7 dias, sem promessa antiga ────────────

def test_troca_sete_dias_sem_texto_de_trinta(client, db, make_product):
    home = _get(client, "/")
    assert "30 dias" not in home
    assert "Troca em até 7 dias" in home

    login = _get(client, "/login")
    assert "30 dias" not in login
    assert "Troca em até 7 dias" in login

    product = make_product()
    pagina = _get(client, f"/produtos/{product.id}")
    assert "30 dias" not in pagina
    assert "Troca fácil em até 7 dias" not in pagina
    assert (
        "Solicite a troca em até 7 dias; a troca gratuita será combinada com o atendimento"
        in pagina
    )


def test_parcelamento_sem_promessa_fixa(client, db):
    for path in ("/", "/login"):
        html = _get(client, path)
        assert "12× sem juros" not in html
        assert "12x sem juros" not in html
        assert "3x de R$" not in html
    home = _get(client, "/")
    assert "Mercado Pago" in home


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


def test_rodape_social_conhecida_usa_png_local_e_nao_sprite(
    client, db, _footer_fresco, _links_proprios
):
    """Instagram/Facebook/Twitter renderizam o PNG versionado no repositório.

    Regressão: o rodapé usava <use href="#icon-..."> apontando para um sprite
    inexistente, então o link existia no HTML mas o glifo não aparecia.
    """
    from loja.modules.models import SocialLink

    _criar(
        SocialLink, db, _links_proprios,
        label="Instagram", url="https://www.instagram.com/pollo.modaspmw",
        icon="fab fa-instagram",
    )
    html = _get(client, "/")

    # O link continua alcançável e com nome acessível.
    assert 'href="https://www.instagram.com/pollo.modaspmw"' in html
    assert 'aria-label="Instagram"' in html

    # Ícone é o PNG local, não o <use> de sprite inexistente.
    assert "img/rodape/instagram.png" in html
    assert 'class="footer-social-icon"' in html
    assert "#icon-instagram" not in html
    assert "<use href=" not in html


def test_rodape_social_desconhecida_cai_para_rotulo_visivel(
    client, db, _footer_fresco, _links_proprios
):
    """Rede sem PNG local não pode sumir: mostra o rótulo dentro do círculo."""
    from loja.modules.models import SocialLink

    _criar(
        SocialLink, db, _links_proprios,
        label="TikTok", url="https://www.tiktok.com/pollomodas",
    )
    html = _get(client, "/")

    assert 'href="https://www.tiktok.com/pollomodas"' in html
    # Rótulo presente no link do rodapé (fallback textual).
    assert 'aria-label="TikTok"' in html
    assert "footer-social-label" in html
    # Não há tentativa de carregar um PNG inexistente.
    assert "img/rodape/tiktok.png" not in html


def test_admin_salvar_social_invalida_cache_do_rodape(
    client, db, admin_user, _footer_fresco, _links_proprios
):
    """Salvar no admin aparece na hora, sem esperar o TTL de 600s.

    O rodapé e a lista do próprio dashboard leem o mesmo cache: sem
    invalidação, o link recém-criado ficava invisível nos dois lugares.
    """
    # Esquece o rodapé em cache ANTES do POST (simula página já visitada).
    from loja.modules.models import SocialLink

    _get(client, "/")
    assert "footer_data" in _cache_mod._cache

    resp = _post_admin(
        client, admin_user, "/admin/add_social_link",
        {
            "label": "Instagram",
            "url": "https://www.instagram.com/pollo.modaspmw",
            "icon": "fab fa-instagram",
        },
    )
    assert resp.status_code in (302, 303), resp.status_code

    criado = SocialLink.query.filter_by(label="Instagram").first()
    assert criado is not None
    _links_proprios.append((criado.id, SocialLink))

    # Imediatamente após o POST: cache descartado e já renderizando.
    assert "footer_data" not in _cache_mod._cache
    html = _get(client, "/")
    assert 'href="https://www.instagram.com/pollo.modaspmw"' in html
    assert "img/rodape/instagram.png" in html


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


def test_home_prazo_convertido_para_sao_paulo(app):
    # Banco guarda UTC naive: 15:00 UTC == 12:00 em America/Sao_Paulo.
    fim_utc = datetime(2026, 1, 15, 15, 0)
    html = _render_home(
        app, [_promo_transiente(_produto_transiente(), ends_at=fim_utc)]
    )
    assert "Termina em 15/01 às 12:00" in html
    assert "15/01 às 15:00" not in html
    assert "data-end=" not in html
    assert "dealsCountdown" not in html


def test_home_promos_mistas_um_prazo_por_card(app):
    com_prazo = _promo_transiente(
        _produto_transiente("Com Prazo"),
        ends_at=datetime(2026, 3, 10, 15, 0),
    )
    sem_prazo = _promo_transiente(_produto_transiente("Sem Prazo"), ends_at=None)
    html = _render_home(app, [com_prazo, sem_prazo])
    assert html.count("Termina em") == 1
    assert "Termina em 10/03 às 12:00" in html
    assert "Com Prazo" in html and "Sem Prazo" in html
    assert "data-end=" not in html
    assert "Acaba em" not in html


def test_titulo_do_dia_so_quando_todas_terminam_hoje(app):
    html = _render_home(
        app, [_promo_transiente(_produto_transiente(), ends_at=None)],
        deals_do_dia=False,
    )
    assert "deals-title" in html
    assert '<span class="flash">do dia</span>' not in html

    html = _render_home(
        app, [_promo_transiente(_produto_transiente(), ends_at=None)],
        deals_do_dia=True,
    )
    assert '<span class="flash">do dia</span>' in html


def test_deals_do_dia_exige_todas_terminando_hoje():
    from loja.modules.services import deals_show_do_dia

    agora = datetime(2026, 6, 10, 12, 0)  # 09:00 em São Paulo
    assert deals_show_do_dia([], agora) is False
    assert deals_show_do_dia(
        [_promo_transiente(_produto_transiente(), ends_at=None)], agora
    ) is False
    assert deals_show_do_dia(
        [_promo_transiente(_produto_transiente(), ends_at=datetime(2026, 6, 10, 20, 0))],
        agora,
    ) is True
    assert deals_show_do_dia(
        [
            _promo_transiente(_produto_transiente(), ends_at=datetime(2026, 6, 10, 20, 0)),
            _promo_transiente(_produto_transiente(), ends_at=datetime(2026, 6, 11, 15, 0)),
        ],
        agora,
    ) is False


def test_rodape_rejeita_rota_exclusiva_de_post(app):
    from loja.modules.footer_links import is_safe_footer_url

    with app.test_request_context("/"):
        assert is_safe_footer_url("/sobre") is True
        assert is_safe_footer_url(f"/produtos/{uuid.uuid4()}/favoritar") is False
