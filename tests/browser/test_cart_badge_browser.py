"""Navegador real (Playwright/Chromium): badge de quantidade da sacola.

Pula localmente sem `playwright`; roda no CI dedicado com banco
descartável e dados sintéticos (workflow browser-cart-badge.yml).
Somente leitura da loja + escrita de carrinho sintético do cliente de
teste; nenhum pagamento, checkout ou e-mail.
"""
import os
import re
import uuid
from decimal import Decimal

import pytest

playwright = pytest.importorskip("playwright.sync_api")

BASE_URL = os.environ.get("BROWSER_BASE_URL", "http://127.0.0.1:5111")
CUSTOMER_EMAIL = "browser.cliente@sintetico.test"
CUSTOMER_PASSWORD = "cliente123"
SHOTS = "/tmp/cartshots"

DESKTOP = {"width": 1366, "height": 900}
MOBILE = {"width": 390, "height": 844}


@pytest.fixture(scope="module")
def browser():
    os.makedirs(SHOTS, exist_ok=True)
    with playwright.sync_playwright() as runner:
        instance = runner.chromium.launch(args=["--no-sandbox"])
        yield instance
        instance.close()


@pytest.fixture()
def page(browser, request):
    if getattr(request, "param", None) == "mobile" or \
            os.environ.get("BROWSER_MOBILE") == "1":
        viewport = MOBILE
        tag = "mobile"
    else:
        viewport = DESKTOP
        tag = "desktop"
    context = browser.new_context(viewport=viewport)
    tab = context.new_page()
    tab._shot_tag = tag
    tab._shot_w = viewport["width"]
    yield tab
    context.close()


def shot(page, name):
    page.screenshot(path=f"{SHOTS}/{name}-{page._shot_tag}.png",
                    clip={"x": 0, "y": 0, "width": page._shot_w, "height": 140})


def seed_cart(entries):
    """Zera o carrinho do cliente de teste e insere `entries=[(qtd,)]`.

    Escreve direto no banco descartável (mesma fonte do badge, sem
    contagem paralela); o app em teste lê normalmente.
    """
    sys_path = os.getcwd()
    import sys
    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    from run import app
    from loja import db
    from loja.modules.models import Cart, CartItem, Product, User
    with app.app_context():
        customer = User.query.filter_by(email=CUSTOMER_EMAIL).first()
        assert customer is not None, "seed do cliente ausente"
        cart = Cart.query.filter_by(user_id=customer.id).first()
        if cart is None:
            cart = Cart(id=uuid.uuid4(), user_id=customer.id)
            db.session.add(cart)
            db.session.flush()
        CartItem.query.filter_by(cart_id=cart.id).delete()
        prods = []
        for i in range(max(1, len(entries))):
            name = ("Vestido Sintético Browser" if i == 0
                    else "Blusa Sintética Badge")
            prod = Product.query.filter_by(name=name).first()
            if prod is None:
                prod = Product(
                    id=uuid.uuid4(), name=name,
                    description="Produto sintético p/ badge.",
                    price=Decimal("89.90"),
                    image1="https://exemplo.com/badge.png",
                    category="feminino", stock=200, is_active=True)
                db.session.add(prod)
                db.session.flush()
            prods.append(prod)
        for prod, (qty,) in zip(prods, entries):
            db.session.add(CartItem(
                id=uuid.uuid4(), cart_id=cart.id, product_id=prod.id,
                selected_size="M", quantity=qty, unit_price=prod.price))
        db.session.commit()


def csrf_of(html):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "csrf ausente"
    return match.group(1)


def customer_login(page):
    page.goto(BASE_URL + "/login")
    card = ".auth-card "
    page.fill(card + 'input[name="email"]', CUSTOMER_EMAIL)
    page.fill(card + 'input[name="password"]', CUSTOMER_PASSWORD)
    page.evaluate(
        """(tok) => {
          document.querySelector('.auth-card input[name="csrf_token"]').value = tok;
        }""",
        csrf_of(page.content()),
    )
    with page.expect_navigation():
        page.click('.auth-card button[type="submit"]')


def badge_text(page):
    return page.evaluate(
        "document.querySelector('.cart-badge')?.textContent ?? null")


def cart_label(page):
    return page.evaluate(
        "document.querySelector('.navbar-cart')"
        ".getAttribute('aria-label')")


def test_vazio_sem_badge(page):
    seed_cart([])
    page.goto(BASE_URL + "/")
    assert badge_text(page) is None
    assert cart_label(page) == "Sacola"
    shot(page, "badge-vazio")


def test_uma_unidade(page):
    seed_cart([(1,)])
    customer_login(page)
    page.goto(BASE_URL + "/")
    assert badge_text(page) == "1"
    assert cart_label(page) == "Carrinho com 1 item"


def test_soma_quantidades_repetidas(page):
    seed_cart([(3,), (3,)])
    customer_login(page)
    page.goto(BASE_URL + "/")
    assert badge_text(page) == "6"
    assert cart_label(page) == "Carrinho com 6 itens"
    shot(page, "badge-preenchido")
    # Recarregar mantém.
    page.reload()
    assert badge_text(page) == "6"
    # Ir ao carrinho e voltar pelo histórico mantém.
    page.goto(BASE_URL + "/carrinho")
    page.go_back()
    page.wait_for_timeout(800)
    assert badge_text(page) == "6"


def test_remocao_via_ui_oculta_badge(page):
    seed_cart([(2,)])
    customer_login(page)
    page.goto(BASE_URL + "/carrinho")
    assert badge_text(page) == "2"
    with page.expect_navigation():
        page.click(".remove-form .remove-btn")
    assert badge_text(page) is None
    assert cart_label(page) == "Sacola"


def test_acima_de_99_exibe_99_mais(page):
    seed_cart([(99,), (5,)])
    customer_login(page)
    page.goto(BASE_URL + "/")
    assert badge_text(page) == "99+"


def test_badge_sem_sobreposicao(page):
    seed_cart([(3,), (3,)])
    customer_login(page)
    page.goto(BASE_URL + "/")
    box = page.locator(".cart-badge").bounding_box()
    assert box and box["width"] > 0 and box["height"] > 0
    assert box["x"] >= 0 and box["y"] >= 0
    header = page.locator(".navbar").bounding_box()
    assert box["x"] + box["width"] <= header["x"] + header["width"] + 1
    assert box["y"] + box["height"] <= header["y"] + header["height"] + 1
    icons = []
    for loc in page.locator(".navbar-actions .navbar-icon").all():
        icons.append(loc.bounding_box())
    cart_box = page.locator(".navbar-actions .navbar-cart").bounding_box()
    assert len(icons) >= 2
    for other in icons:
        if other is None:
            continue
        if (abs(other["x"] - cart_box["x"]) < 1
                and abs(other["y"] - cart_box["y"]) < 1):
            continue  # o próprio ícone da sacola (contém o badge)
        overlap = not (box["x"] + box["width"] <= other["x"] or
                       other["x"] + other["width"] <= box["x"] or
                       box["y"] + box["height"] <= other["y"] or
                       other["y"] + other["height"] <= box["y"])
        assert not overlap, f"sobreposição com {other}"
