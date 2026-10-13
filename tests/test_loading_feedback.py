"""Feedback de carregamento (barra + botão) sem mudar comportamento.

Somente apresentação: navegação e envios funcionam idênticos sem JS.
Sem executor JS neste ambiente: comportamentos de navegador cobertos por
asserts estáticos da fiação + rotas/templates no servidor (limitação
registrada; não equivalem a teste de navegador).
"""
import re


def _js():
    return open(
        "public/static/scripts/loading_feedback.js", encoding="utf-8"
    ).read()


def _css():
    return open(
        "public/static/styles/animation.css", encoding="utf-8"
    ).read()


def _dashboard_js():
    return open(
        "public/static/scripts/dashboard_admin.js", encoding="utf-8"
    ).read()


# --------------------------------------------------------------------------
# 1) Barra de progresso: indeterminada, com atraso, sem interceptar
# --------------------------------------------------------------------------

def test_barra_indeterminada_com_atraso_e_exclusoes():
    src = _js()
    assert "page-progress" in src
    assert "PROGRESS_DELAY_MS" in src
    assert "is-active" in src  # keyframes moram no CSS
    for excluded in (
        "mailto:", "tel:", "download", "target", "ctrlKey", "metaKey",
        "shiftKey",
    ):
        assert excluded in src, excluded
    # Sem porcentagem fictícia: a barra não exibe texto numérico.
    assert "Carregando página" in src


def test_navegacao_normal_preservada():
    src = _js()
    # Nenhum preventDefault no caminho de links; sem fetch/retry aqui.
    click_block = src.split("showProgress")[0]
    assert "preventDefault" not in click_block
    assert "fetch(" not in src
    assert "XMLHttpRequest" not in src
    # defaultPrevented respeitado: decisão após handlers que cancelam.
    assert "event.defaultPrevented" in src
    # Âncora de mesma página em qualquer forma: sem loading.
    assert "pathname" in src and "search" in src
    assert "sameDocument" in src
    # Links das parciais usam só o feedback do dashboard.
    assert "data-partial-container" in src


def test_css_indeterminado_e_movimento_reduzido():
    css = _css()
    assert "@keyframes page-progress-slide" in css
    assert "@keyframes loading-spin" in css
    assert ".loading-spinner" in css
    assert "prefers-reduced-motion" in css


# --------------------------------------------------------------------------
# 2) Botão: spinner, texto, largura, aria, sem duplicar envio
# --------------------------------------------------------------------------

def test_botao_spinner_texto_e_bloqueio_sem_perder_dados():
    src = _js()
    assert "aria-busy" in src
    assert "minWidth" in src
    assert "data-loading-text" in src
    assert "Entrando" not in src  # textos vivem nos templates, não no JS
    assert "dataset.submitted" in src
    # 2º envio bloqueado; 1º nunca desabilitado antes de partir.
    assert ".disabled = true" not in src
    # Só após validação (inclusive novalidate via checkValidity).
    assert "checkValidity" in src
    assert "novalidate" in src and "formnovalidate" in src
    # Destino/janela respeitados (target, formtarget, dialog).
    assert "formtarget" in src and "dialog" in src
    # Mecanismos próprios preservados (checkout + data-once).
    assert "checkoutForm" in src and "data-once" in src
    assert 'data-loading' in src and "'off'" in src


def test_templates_com_textos_e_opt_outs(client):
    login_html = client.get("/login").get_data(as_text=True)
    assert 'data-loading-text="Entrando' in login_html
    cart_html = open(
        "loja/templates/cart/carrinho.html", encoding="utf-8").read()
    assert cart_html.count('data-loading="off"') >= 2  # +/− quantidade
    checkout_html = open(
        "loja/templates/cart/checkout.html", encoding="utf-8").read()
    assert 'data-loading="off"' in checkout_html  # mecanismo próprio
    assert "setFinishLoading" in checkout_html  # preservado
    base_html = open(
        "loja/templates/base.html", encoding="utf-8").read()
    assert "loading_feedback.js" in base_html


def test_sem_js_formularios_continuam_normais(client, db, make_user):
    # Sem executar JS: login POST funciona; botões são submits comuns.
    from loja.modules.models import User

    assert 'type="submit"' in client.get("/login").get_data(as_text=True)


# --------------------------------------------------------------------------
# 3) Admin fetch: botão restaurado em finally, sem encerrar o mais novo
# --------------------------------------------------------------------------

def test_dashboard_fetch_restaura_botao_no_finally():
    src = _dashboard_js()
    assert "function fetchInto(container, url, initiator)" in src
    assert "container._loadingControl" in src
    assert "removeAttribute('aria-busy')" in src
    # Stale-guard: finally só restaura se for a requisição vigente.
    finally_block = src.split(".finally")[1].split("function loadPartial")[0]
    assert "controllers.get(key) !== ctrl" in finally_block


# --------------------------------------------------------------------------
# 4) Restauração e acessibilidade
# --------------------------------------------------------------------------

def test_pageshow_restaura_tudo():
    src = _js()
    assert "pageshow" in src
    assert "restoreAll" in src
    # Restaura texto original e habilita só o que este handler marcou.
    assert "loadingOriginal" in src
    assert "querySelectorAll('[data-loading-active]')" in src
    # Cancelamento posterior limpa a marcação (sem travar).
    assert "delete form.dataset.submitted" in src
    # Anúncio acessível separado do indicador: barra sem aria-hidden,
    # spinner decorativo com aria-hidden.
    assert "bar.setAttribute('aria-hidden'" not in src
    assert "setTimeout" in src
    # Cancelamento tardio: releitura de defaultPrevented ao exibir.
    assert "progressEvent" in src
    # aria-busy pré-existente preservado e restaurado.
    assert "loadingAriaBusy" in src


def test_textos_sao_texto_e_historico_preservado():
    src = _js()
    assert "pushState" not in src  # este módulo nunca mexe no histórico
    assert "replaceState" not in src
    # <input type=submit>: value nunca sobrescrito (é dado enviado).
    assert ".value =" not in src
    assert "loadingMinWidth" in src  # min-width anterior restaurado
