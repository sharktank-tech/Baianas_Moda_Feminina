"""Navegador real (Playwright/Chromium): opt-out da newsletter.

Cobre desktop e mobile: GET de confirmacao sem alteracao, POST de
cancelamento, reativacao, token invalido, repeticao de GET e cadastro
repetido que nao reativa. O token sintetico vem do seed
(seed_browser_qa.py -> /tmp/browser_newsletter_token.txt).

O estado persistido e verificado por consulta direta ao banco
descartavel do ambiente de teste (DATABASE_URL/POLLOMODAS_SCHEMA do
job) — sem endpoint publico para expor inscricoes.
"""
import os
import uuid

import pytest

playwright = pytest.importorskip("playwright.sync_api")

BASE_URL = os.environ.get("BROWSER_BASE_URL", "http://127.0.0.1:5111")
TOKEN_FILE = "/tmp/browser_newsletter_token.txt"
DB_SCHEMA = os.environ.get("POLLOMODAS_SCHEMA", "pollomodas")

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


@pytest.fixture(scope="module")
def subscriber_state():
    """Consulta direta ao banco descartavel do ambiente de teste.

    Retorna funcao que le (unsubscribed_at, resubscribed_at) por id do
    assinante. So funciona com DATABASE_URL de Postgres descartavel.
    """
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgres"):
        pytest.skip("sem DATABASE_URL Postgres para consulta de estado")

    import psycopg2
    from sqlalchemy.engine import make_url

    url = make_url(database_url)

    def query(subscriber_id_hex):
        conn = psycopg2.connect(
            host=url.host, port=url.port or 5432, dbname=url.database,
            user=url.username, password=url.password,
        )
        try:
            cur = conn.cursor()
            cur.execute(
                f"SELECT unsubscribed_at, resubscribed_at "
                f"FROM {DB_SCHEMA}.newsletter_subscribers WHERE id = %s",
                (uuid.UUID(hex=subscriber_id_hex),),
            )
            return cur.fetchone()
        finally:
            conn.close()

    return query


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


def _id_hex(token):
    return token.split(".", 1)[0]


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_get_confirmacao_sem_alteracao(page, token, subscriber_state):
    """GET renderiza a pagina de confirmacao sem alterar o banco."""
    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    assert page.wait_for_selector("form", timeout=15000)
    assert "cancelar" in _body_text(page)
    row = subscriber_state(_id_hex(token))
    assert row is not None
    assert row[0] is None  # unsubscribed_at intacto


@pytest.mark.parametrize("page", ["mobile"], indirect=True)
def test_get_mobile(page, token, subscriber_state):
    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    assert page.wait_for_selector("form", timeout=15000)
    row = subscriber_state(_id_hex(token))
    assert row is not None and row[0] is None


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_post_cancelar_persiste(page, token, subscriber_state):
    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    page.click("form:has(input[value='cancelar']) button[type='submit']")
    page.wait_for_selector(".nl-cancel-ok", timeout=15000)
    assert "cancelada" in _body_text(page)
    row = subscriber_state(_id_hex(token))
    assert row is not None and row[0] is not None  # cancelado persistido


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_post_reativar_persiste(page, token, subscriber_state):
    """Cancela, reativa e confirma o estado persistido no banco."""
    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    page.click("form:has(input[value='cancelar']) button[type='submit']")
    page.wait_for_selector(".nl-cancel-ok", timeout=15000)
    row = subscriber_state(_id_hex(token))
    assert row is not None and row[0] is not None

    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    page.click("form:has(input[value='reativar']) button[type='submit']")
    page.wait_for_selector(".nl-cancel-ok", timeout=15000)
    assert "ativa" in _body_text(page)
    row = subscriber_state(_id_hex(token))
    assert row is not None
    assert row[0] is None          # unsubscribed_at limpo
    assert row[1] is not None      # resubscribed_at registrado


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_token_invalido(page, subscriber_state):
    page.goto(f"{BASE_URL}/newsletter/cancelar/token-invalido-abc123")
    page.wait_for_selector(".nl-cancel-card", timeout=15000)
    assert "link inválido" in _body_text(page)
    assert page.query_selector("form:has(input[value='cancelar'])") is None
    assert subscriber_state("0" * 32) is None  # id inexistente: nada no banco


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_get_repetido_nao_altera(page, token, subscriber_state):
    """GET seguido de GET apos POST nao reverte o estado persistido."""
    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    page.click("form:has(input[value='cancelar']) button[type='submit']")
    page.wait_for_selector(".nl-cancel-ok", timeout=15000)
    assert subscriber_state(_id_hex(token))[0] is not None

    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    assert page.wait_for_selector("form", timeout=15000)
    assert page.query_selector(
        "form:has(input[value='reativar'])") is not None
    # GET nao altera: cancelamento permanece no banco
    assert subscriber_state(_id_hex(token))[0] is not None


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_cadastro_repetido_preserva_cancelamento(
        page, token, subscriber_state):
    """Cadastro repetido do e-mail cancelado nao reativa: cancelamento
    persistido confirmado por consulta ao banco (nao so por UI)."""
    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    page.click("form:has(input[value='cancelar']) button[type='submit']")
    page.wait_for_selector(".nl-cancel-ok", timeout=15000)
    assert subscriber_state(_id_hex(token))[0] is not None

    page.goto(f"{BASE_URL}/")
    page.fill(".newsletter-form input[name='email']",
              "browser.newsletter@sintetico.test")
    page.click(".newsletter-form button[type='submit']")
    page.wait_for_url("**/#newsletter", timeout=15000)
    assert page.query_selector(".newsletter-feedback") is None

    # Prova de persistencia: o banco continua com o cancelamento.
    row = subscriber_state(_id_hex(token))
    assert row is not None and row[0] is not None

    # E o link ainda oferece reativacao (estado cancelado, nao apagado).
    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    assert page.query_selector(
        "form:has(input[value='reativar'])") is not None


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_cancelar_reativar_cancelar_mesmo_token(
        page, token, subscriber_state):
    """Mesmo link: cancelar → reativar → cancelar de novo, com banco."""
    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    page.click("form:has(input[value='cancelar']) button[type='submit']")
    page.wait_for_selector(".nl-cancel-ok", timeout=15000)
    assert subscriber_state(_id_hex(token))[0] is not None

    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    page.click("form:has(input[value='reativar']) button[type='submit']")
    page.wait_for_selector(".nl-cancel-ok", timeout=15000)
    row = subscriber_state(_id_hex(token))
    assert row is not None and row[0] is None and row[1] is not None

    page.goto(f"{BASE_URL}/newsletter/cancelar/{token}")
    page.click("form:has(input[value='cancelar']) button[type='submit']")
    page.wait_for_selector(".nl-cancel-ok", timeout=15000)
    assert subscriber_state(_id_hex(token))[0] is not None
