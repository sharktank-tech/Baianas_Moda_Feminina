"""Navegador real (Playwright/Chromium): opt-out da newsletter.

Cobre desktop e mobile: GET de confirmacao sem alteracao, POST de
cancelamento, reativacao, token invalido, repeticao de GET e cadastro
repetido que nao reativa. O token sintetico vem do seed
(seed_browser_qa.py -> /tmp/browser_newsletter_token.txt).
"""
import os

import pytest

playwright = pytest.importorskip("playwright.sync_api")

BASE_URL = os.environ.get("BROWSER_BASE_URL", "http://127.0.0.1:5111")
TOKEN_FILE = "/tmp/browser_newsletter_token.txt"

DESKTOP = {"width": 1366, "height": 900}
MOBILE = {"width": 390, "height": 844}


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as runner:
        instance = runner.chromium.launch(args=["--no-sandbox"])
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def token():
    with open(TOKEN_FILE, encoding="utf-8") as f:
        return f.read().strip()


@pytest.fixture()
def page(browser, request):
    if getattr(request, "param", None) == "mobile" or \
            os.environ.get("BROWSER_MOBILE") == "1":
        viewport = MOBILE
    else:
        viewport = DESKTOP
    context = browser.new_context(viewport=viewport)
    tab = context.new_page()
    yield tab
    context.close()


def _body_text(page):
    return (page.text_content("body") or "").lower()


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_get_confirmacao_sem_alteracao(page, token):
    """GET renderiza a pagina de confirmacao com os dois formularios."""
    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    assert page.wait_for_selector("form", timeout=15000)
    assert "cancelar" in _body_text(page)


@pytest.mark.parametrize("page", ["mobile"], indirect=True)
def test_get_mobile(page, token):
    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    assert page.wait_for_selector("form", timeout=15000)


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_post_cancelar(page, token):
    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    page.click("form:has(input[value='cancelar']) button[type='submit']")
    page.wait_for_selector(".nl-cancel-ok", timeout=15000)
    assert "cancelada" in _body_text(page)


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_post_reativar_apos_cancelamento(page, token):
    """Cancela e em seguida reativa pelo mesmo link."""
    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    page.click("form:has(input[value='cancelar']) button[type='submit']")
    page.wait_for_selector(".nl-cancel-ok", timeout=15000)

    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    page.click("form:has(input[value='reativar']) button[type='submit']")
    page.wait_for_selector(".nl-cancel-ok", timeout=15000)
    assert "ativa" in _body_text(page)


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_token_invalido(page):
    page.goto(f"{BASE_URL}/newsletter/cancelar/token-invalido-abc123")
    page.wait_for_selector(".nl-cancel-card", timeout=15000)
    assert "link inválido" in _body_text(page)
    # O formulario de confirmacao NAO deve aparecer em token invalido
    assert page.query_selector("form:has(input[value='cancelar'])") is None


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_get_repetido_nao_altera(page, token):
    """GET seguido de GET apos POST nao reverte o estado."""
    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    page.click("form:has(input[value='cancelar']) button[type='submit']")
    page.wait_for_selector(".nl-cancel-ok", timeout=15000)

    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    assert page.wait_for_selector("form", timeout=15000)
    # Reativacao continua disponivel: GET nao altera nada
    assert page.query_selector("form:has(input[value='reativar'])") is not None


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_cadastro_repetido_nao_reativa(page, token):
    """Cadastro repetido do e-mail cancelado nao reativa nem mostra sucesso."""
    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    page.click("form:has(input[value='cancelar']) button[type='submit']")
    page.wait_for_selector(".nl-cancel-ok", timeout=15000)

    page.goto(f"{BASE_URL}/")
    page.fill(".newsletter-form input[name='email']",
              "browser.newsletter@sintetico.test")
    page.click(".newsletter-form button[type='submit']")
    page.wait_for_url("**/#newsletter", timeout=15000)
    # Mensagem de sucesso (category 'newsletter') NAO deve aparecer:
    # o cadastro de cancelado nao foi aceito nem reativado.
    assert page.query_selector(".newsletter-feedback") is None

    # O cancelamento permanece: GET do link ainda oferece reativacao.
    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    assert page.query_selector(
        "form:has(input[value='reativar'])") is not None
