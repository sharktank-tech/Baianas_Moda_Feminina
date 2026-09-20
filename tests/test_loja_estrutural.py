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
from decimal import Decimal

import pytest


def _get(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, f"GET {path} retornou {resp.status_code}"
    return resp.get_data(as_text=True)


@pytest.fixture
def _sem_promocoes(db):
    """Isola os testes de vitrine de Promotion criada por outros arquivos."""
    from loja.modules.models import Promotion

    Promotion.query.delete()
    db.session.commit()
    yield
    Promotion.query.delete()
    db.session.commit()


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


def test_sobre_cta_leva_a_listagem(client, db):
    html = _get(client, "/sobre")
    match = re.search(
        r'<a href="([^"]+)" class="sobre-cta-btn">\s*Ver coleção',
        html,
    )
    assert match, "CTA 'Ver coleção' não encontrado no Sobre."
    assert match.group(1) == "/produtos"


def test_home_sem_promocao_sem_cronometro_nem_titulo_urgencia(client, db, _sem_promocoes):
    html = _get(client, "/")
    assert "data-end=" not in html
    assert "deals-countdown" not in html
    assert "deals-title" not in html
    assert "chegam já já" in html


def test_home_com_promocao_tem_cronometro_e_titulo(client, db, make_product):
    from loja.modules.models import Promotion
    from loja.utils import utcnow

    product = make_product()
    promo = Promotion(
        id=uuid.uuid4(),
        product_id=product.id,
        sale_price=Decimal("39.90"),
        is_active=True,
        starts_at=None,
        ends_at=None,
    )
    db.session.add(promo)
    db.session.commit()

    from datetime import datetime

    html = _get(client, "/")
    assert "data-end=" in html
    assert "deals-title" in html
    assert "deals-countdown" in html

    # Sem prazo real (ends_at None) o cronômetro usa o fim do dia.
    match = re.search(r'data-end="([^"]+)"', html)
    assert match
    assert datetime.fromisoformat(match.group(1)).date() == utcnow().date()
