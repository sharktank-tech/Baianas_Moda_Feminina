"""Navegador real (Playwright/Chromium): feedback de carregamento.

Pula localmente sem `playwright` instalado; roda no CI dedicado com banco
descartável e dados sintéticos (ver seed_browser_qa.py + browser-loading.yml).
Nenhum pagamento, escrita real em produção ou e-mail: os POSTs vão a /login
(credenciais inválidas, exceto o login válido inicial) e os GETs do admin
são somente leitura.
"""
import os
import re

import pytest

playwright = pytest.importorskip("playwright.sync_api")

BASE_URL = os.environ.get("BROWSER_BASE_URL", "http://127.0.0.1:5111")
ADMIN_EMAIL = os.environ["BROWSER_ADMIN_EMAIL"]
ADMIN_PASSWORD = os.environ["BROWSER_ADMIN_PASSWORD"]

DESKTOP = {"width": 1366, "height": 900}
MOBILE = {"width": 390, "height": 844}


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as runner:
        instance = runner.chromium.launch(args=["--no-sandbox"])
        yield instance
        instance.close()


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


printed_errors = []


def _hook_console(page):
    page.on("pageerror", lambda exc: printed_errors.append(f"pageerror: {exc}"))
    page.on("console", lambda msg: printed_errors.append(
        f"console.{msg.type}: {msg.text}") if msg.type == "error" else None)


@pytest.fixture()
def watched_page(page):
    _hook_console(page)
    yield page
    if printed_errors:
        print("\nCONSOLE:", printed_errors)
    del printed_errors[:]


def test_js_carregou_sem_erros(watched_page):
    watched_page.goto(BASE_URL + "/login")
    watched_page.wait_for_timeout(500)
    assert watched_page.evaluate("window.__loadingFeedback === 1")
    # Erros preexistentes (ads, outros scripts) fora do escopo: só falha
    # se o erro mencionar este módulo.
    novas = [e for e in printed_errors if "loading_feedback" in e]
    assert novas == []


def csrf_of(html):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "csrf ausente"
    return match.group(1)


def fill_login(page, email, password):
    page.goto(BASE_URL + "/login")
    card = ".auth-card "
    page.fill(card + 'input[name="email"]', email)
    page.fill(card + 'input[name="password"]', password)
    page.evaluate(
        """(tok) => {
          document.querySelector('.auth-card input[name="csrf_token"]').value = tok;
        }""",
        csrf_of(page.content()),
    )


def admin_login(page):
    fill_login(page, ADMIN_EMAIL, ADMIN_PASSWORD)
    with page.expect_navigation():
        page.click('.auth-card button[type="submit"]')
    assert "/conta" in page.url


# --------------------------------------------------------------------------
# 1) Barra: navegação normal mostra; cancelada e âncora, não
# --------------------------------------------------------------------------

@pytest.mark.parametrize("page", ["mobile"], indirect=True)
def test_nav_normal_arma_barra(page):
    # O clique decide armar (rastro em sessionStorage, mesma aba) e a
    # navegação conclui; a barra some com o documento novo. Em loopback
    # o commit (<150ms) precede o anti-flash por desenho — o rastro prova
    # a decisão sem correr contra o commit.
    page.goto(BASE_URL + "/login")
    page.eval_on_selector('a[href$="/register"]', "a => a.click()")
    page.wait_for_url("**/register", timeout=15000)
    assert page.evaluate("sessionStorage.getItem('__lfArm')") == "1"
    assert page.evaluate(
        "!!document.querySelector('#page-progress.is-active')") is False


def test_nav_normal_arma_barra_desktop(page):
    page.goto(BASE_URL + "/login")
    page.eval_on_selector('a[href$="/register"]', "a => a.click()")
    page.wait_for_url("**/register", timeout=15000)
    assert page.evaluate("sessionStorage.getItem('__lfArm')") == "1"
    assert page.evaluate(
        "!!document.querySelector('#page-progress.is-active')") is False


def test_clique_cancelado_tardio_sem_barra(page):
    page.goto(BASE_URL + "/login")
    # Handler registrado DEPOIS do módulo (que carrega no <head> com defer):
    # cancela após a decisão inicial da barra.
    page.evaluate("""() => {
      document.addEventListener('click', (e) => {
        const a = e.target.closest('a[href$="/register"]');
        if (a) e.preventDefault();
      });
    }""")
    page.click('a[href$="/register"], a[href="/register"]')
    page.wait_for_timeout(600)
    assert page.evaluate(
        "!!document.querySelector('#page-progress.is-active')") is False
    assert page.url.endswith("/login")


def test_ancoras_absoluta_e_relativa_sem_loading(page):
    page.goto(BASE_URL + "/login#secao")
    assert page.evaluate(
        "!!document.querySelector('#page-progress.is-active')") is False
    page.goto(BASE_URL + "/login")
    page.evaluate(
        "() => { history.pushState(null, '', '/login#outra'); }")
    page.wait_for_timeout(300)
    assert page.evaluate(
        "!!document.querySelector('#page-progress.is-active')") is False
    absolute = BASE_URL + "/login#terceira"
    page.evaluate("(url) => { location.href = url; }", absolute)
    page.wait_for_timeout(300)
    assert page.evaluate(
        "!!document.querySelector('#page-progress.is-active')") is False
    assert page.evaluate("sessionStorage.getItem('__lfArm')") is None


def test_ctrl_clique_sem_barra(page):
    page.goto(BASE_URL + "/")
    page.click('a[href="/produtos"]', modifiers=["Control"])
    page.wait_for_timeout(400)
    assert page.evaluate(
        "!!document.querySelector('#page-progress.is-active')") is False
    assert page.evaluate("sessionStorage.getItem('__lfArm')") is None


# --------------------------------------------------------------------------
# 2) Formulários: válido/inválido, novalidate, diálogo, nova aba
# --------------------------------------------------------------------------

def fulfill_204(page, pattern, sink):
    """POSTs registrados e concluídos com 204: sem navegar, o documento
    permanece vivo e toda asserção de marcação é livre de corrida."""

    def handle(route):
        sink.append(route.request.post_data or "1")
        route.fulfill(status=204)

    page.route(pattern, handle)


def _dynamic_form(page, *, action="/login", method="post", novalidate=False,
                  target=None, buttons=(("a", "A"), ("b", "B"))):
    page.goto(BASE_URL + "/login")
    target_attr = f'target="{target}"' if target else ""
    noval = "novalidate" if novalidate else ""
    btns = "".join(
        f'<button type="submit" name="acao" value="{v}">{t}</button>'
        for v, t in buttons)
    page.evaluate(
        """([action, method, noval, target, btns, csrf]) => {
          const f = document.createElement('form');
          f.id = 'dyn'; f.method = method; f.action = action;
          if (noval) f.setAttribute('novalidate', '');
          if (target) f.setAttribute('target', target);
          f.innerHTML = btns + `<input type="hidden" name="csrf_token"
            value="${csrf}">`;
          document.body.appendChild(f);
        }""",
        [action, method, noval, target or "", btns,
         csrf_of(page.content())])


def test_submit_valido_marca_e_bloqueia_duplo(page):
    posts = []
    _dynamic_form(page)
    fulfill_204(page, "**/login", posts)
    page.eval_on_selector("#dyn button[value='b']", "b => b.click()")
    page.wait_for_function(
        "() => document.querySelector('#dyn button[value=\"b\"]')"
        ".getAttribute('aria-busy') === 'true'",
        timeout=5000)
    assert page.evaluate(
        "document.querySelector('#dyn').dataset.submitted") == "1"
    # 2º clique no botão desabilitado: sem efeito.
    page.eval_on_selector("#dyn button[value='b']", "b => b.click()")
    page.wait_for_timeout(1000)
    assert len(posts) == 1
    assert page.evaluate("!!document.querySelector('#dyn')") is True


def test_multiplos_botoes_preservam_name_value(page):
    bodies = []
    _dynamic_form(page)
    fulfill_204(page, "**/login", bodies)
    page.eval_on_selector("#dyn button[value='b']", "b => b.click()")
    page.wait_for_function(
        "() => document.querySelector('#dyn').dataset.submitted === '1'",
        timeout=5000)
    page.wait_for_timeout(1000)
    assert len(bodies) == 1
    assert "acao=b" in bodies[0] and "acao=a" not in bodies[0]


def test_submit_invalido_sem_loading(page):
    _dynamic_form(page)
    page.evaluate("""() => {
      document.querySelector('#dyn').insertAdjacentHTML('afterbegin',
        '<input name="obrig" required value="">');
    }""")
    page.eval_on_selector("#dyn button[value='a']", "b => b.click()")
    page.wait_for_timeout(800)
    assert page.evaluate(
        "document.querySelector('#dyn').dataset.submitted || null") is None
    assert page.evaluate(
        "document.querySelector('#dyn button[value=\"a\"]')"
        ".getAttribute('aria-busy')") is None


def test_novalidate_envia_com_loading(page):
    bodies = []
    _dynamic_form(page, novalidate=True)
    page.evaluate("""() => {
      document.querySelector('#dyn').insertAdjacentHTML('afterbegin',
        '<input name="obrig" required value="">');
    }""")
    fulfill_204(page, "**/login", bodies)
    page.eval_on_selector("#dyn button[value='a']", "b => b.click()")
    page.wait_for_function(
        "() => document.querySelector('#dyn').dataset.submitted === '1'",
        timeout=5000)
    page.wait_for_timeout(1000)
    assert len(bodies) == 1


def test_formnovalidate_passa_com_campo_invalido(page):
    _dynamic_form(page, buttons=(("x", "x"),))
    page.evaluate("""() => {
      const f = document.querySelector('#dyn');
      f.insertAdjacentHTML('afterbegin',
        '<input name="obrig" required value="">');
      f.querySelector('button').setAttribute('formnovalidate', '');
    }""")
    bodies = []
    fulfill_204(page, "**/login", bodies)
    page.eval_on_selector("#dyn button", "b => b.click()")
    page.wait_for_function(
        "() => document.querySelector('#dyn').dataset.submitted === '1'",
        timeout=5000)
    page.wait_for_timeout(1000)
    assert len(bodies) == 1


def test_form_target_blank_e_dialog_sem_loading(page):
    _dynamic_form(page, target="_blank")
    page.eval_on_selector("#dyn button[value='a']", "b => b.click()")
    page.wait_for_timeout(800)
    assert page.evaluate(
        "document.querySelector('#dyn').dataset.submitted || null") is None
    page.evaluate("""() => {
      document.body.insertAdjacentHTML('beforeend',
        `<dialog id="dlg"><form method="dialog"><button value="x">ok</button>
         </form></dialog>`);
      document.querySelector('#dlg').showModal();
    }""")
    page.eval_on_selector("#dlg button", "b => b.click()")
    page.wait_for_timeout(400)
    assert page.evaluate(
        "document.querySelector('#dlg form').dataset.submitted || null") \
        is None


def test_input_submit_nao_tem_value_sobrescrito(page):
    _dynamic_form(page, buttons=())
    page.evaluate("""() => {
      document.querySelector('#dyn').insertAdjacentHTML('beforeend',
        '<input type="submit" name="acao" value="Pagar agora">');
    }""")
    bodies = []
    fulfill_204(page, "**/login", bodies)
    page.eval_on_selector("#dyn input[type='submit']", "b => b.click()")
    page.wait_for_function(
        "() => document.querySelector('#dyn input[type=\"submit\"]')"
        ".getAttribute('aria-busy') === 'true'",
        timeout=5000)
    assert page.evaluate(
        "document.querySelector('#dyn input[type=\"submit\"]')"
        ".previousElementSibling.className === 'loading-spinner'")
    assert page.evaluate(
        "document.querySelector('#dyn input[type=\"submit\"]').value") \
        == "Pagar agora"
    page.wait_for_timeout(1000)
    assert len(bodies) == 1
    assert "acao=Pagar+agora" in bodies[0] or "acao=Pagar agora" in bodies[0]


def test_enter_sem_submitter_explicito(page):
    _dynamic_form(page, buttons=(("vai", "v"),))
    page.evaluate("""() => {
      document.querySelector('#dyn').insertAdjacentHTML('afterbegin',
        '<input name="q" value="x">');
    }""")
    bodies = []
    fulfill_204(page, "**/login", bodies)
    page.eval_on_selector("#dyn input[name='q']", "i => i.focus()")
    # Entrada confiável (submissão implícita real), sem aguardar a
    # navegação segurada pela rota.
    page.locator("#dyn input[name='q']").press("Enter", no_wait_after=True)
    page.wait_for_function(
        "() => (document.querySelector('#dyn button')"
        ".getAttribute('aria-busy') === 'true')",
        timeout=5000)
    page.wait_for_timeout(1000)
    assert len(bodies) == 1


# --------------------------------------------------------------------------
# 3) Histórico: voltar restaura botões
# --------------------------------------------------------------------------

def test_voltar_restaura_botao(page):
    admin_login(page)
    page.go_back()
    page.wait_for_timeout(800)
    assert page.evaluate(
        "document.querySelector('button[type=\"submit\"]"
        "[data-loading-active]')") is None
    assert page.evaluate(
        "!!document.querySelector('#page-progress.is-active')") is False


# --------------------------------------------------------------------------
# 4) Admin fetch: erro, retry, abort e sobreposição
# --------------------------------------------------------------------------

def test_admin_fetch_erro_retry_abort_sobreposicao(page):
    admin_login(page)
    blocked = {"n": 0}

    def handle(route):
        blocked["n"] += 1
        route.abort()

    page.route("**/admin/painel/**", handle)
    page.goto(BASE_URL + "/admin#orders")
    page.wait_for_selector("[data-partial-retry]", timeout=20000)
    assert blocked["n"] >= 1
    page.unroute("**/admin/painel/**")
    page.click("[data-partial-retry]")
    page.wait_for_selector("[data-partial-root='orders'] table",
                           timeout=20000)
    # <=900px a sidebar some e entra o nav mobile (mesmo data-panel).
    wide = (page.viewport_size or {}).get("width", 1366) >= 900
    nav = "aside" if wide else "nav.adm-mobile-nav"
    page.click(f"{nav} button[data-panel='manual-review']")
    page.wait_for_selector("[data-partial-root='manual-review']",
                           timeout=20000)
    assert page.evaluate(
        "document.querySelector('[data-partial-container=\"manual-review\"]')"
        ".dataset.partialLoaded || ''") != ""
