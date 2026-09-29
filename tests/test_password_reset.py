"""Fluxo 'Esqueci minha senha' — cobre o checklist da especificação.

Rede: a suíte bloqueia rede externa (conftest._deny_external_network) e o
envio é capturado por monkeypatch — nenhum e-mail real é enviado aqui.
O roteiro de validação com envio real é descrito no PR (item 5.4).
"""

import re
import threading
from datetime import timedelta

import pytest

from conftest import unique_email

PUBLIC_MESSAGE = (
    "Se houver uma conta elegível com esse e-mail, você receberá as "
    "instruções para redefinir sua senha."
)

_TOKEN_RE = re.compile(r"/redefinir-senha\?token=([A-Za-z0-9_\-]+)")
_FLASH_RE = re.compile(r'<span class="text-sm leading-snug">(.*?)</span>', re.S)


@pytest.fixture(autouse=True)
def _reset_config(app, monkeypatch):
    # Envio síncrono (determinístico) e limites altos: os testes de limite
    # sobrescrevem por teste. APP_PUBLIC_URL é a origem do link (2.8).
    monkeypatch.setitem(app.config, "PASSWORD_RESET_SEND_ASYNC", False)
    monkeypatch.setitem(app.config, "PASSWORD_RESET_IP_LIMIT", 1000)
    monkeypatch.setitem(app.config, "PASSWORD_RESET_EMAIL_LIMIT", 1000)
    monkeypatch.setitem(app.config, "APP_PUBLIC_URL", "https://www.pollomodas.test")


@pytest.fixture
def sent_emails(monkeypatch):
    """Captura os e-mails sem tocar em rede real."""
    sent = []

    def _fake_send(destinatario, assunto, corpo, corpo_html=None):
        sent.append(
            {
                "to": destinatario,
                "assunto": assunto,
                "corpo": corpo,
                "html": corpo_html,
            }
        )

    monkeypatch.setattr("loja.services.password_reset.enviar_email", _fake_send)
    return sent


def _token_from(sent):
    for email in reversed(sent):
        match = _TOKEN_RE.search(email["corpo"]) or _TOKEN_RE.search(
            email["html"] or ""
        )
        if match:
            return match.group(1)
    raise AssertionError("nenhum token encontrado nos e-mails capturados")


def _limpar_usuario_em_g():
    """Simula o AppContext novo de cada request em produção.

    A suíte roda com um AppContext compartilhado (fixture `db`) e o
    Flask-Login cacheia current_user em `g`; sem limpar, o loader não é
    chamado entre requests. Em produção (WSGI/serverless) cada request
    cria AppContext novo → g novo → loader executa.
    """
    from flask import g

    g.pop("_login_user", None)


def _solicitar(client, email, *, headers=None):
    return client.post(
        "/esqueci-senha", data={"email": email}, headers=headers or {}
    )


def _redefinir(client, token, senha, confirm=None):
    return client.post(
        "/redefinir-senha",
        data={
            "token": token,
            "password": senha,
            "confirm": confirm if confirm is not None else senha,
        },
    )


# ── 1. Resposta pública idêntica ──────────────────────────────────────────────

def test_resposta_identica_email_existente_inexistente_e_sem_senha_local(
    client, make_user, sent_emails
):
    """Existente, inexistente e conta sem senha local (OAuth-only) → idêntico."""
    existente = make_user()
    oauth_only = make_user()
    # Guarda da política 0.3: conta sem senha local não é elegível e nunca
    # ganha senha criada por recuperação (password vazio = sem credencial).
    oauth_only.password = ""
    from loja import db

    db.session.commit()

    inexistente = unique_email("nao-existe")

    codes, locations, flashs = [], [], []
    for email in (existente.email, inexistente, oauth_only.email):
        resp = _solicitar(client, email)
        codes.append(resp.status_code)
        locations.append(resp.headers.get("Location"))
        # Renderiza o flash imediatamente para comparar as mensagens.
        pagina = client.get(resp.headers["Location"])
        flashs.append(
            _FLASH_RE.search(pagina.get_data(as_text=True)).group(1)
        )

    assert codes == [302, 302, 302]
    assert len(set(locations)) == 1
    assert flashs == [PUBLIC_MESSAGE, PUBLIC_MESSAGE, PUBLIC_MESSAGE]

    # Só a conta existente com senha local recebe e-mail.
    assert len(sent_emails) == 1
    assert sent_emails[0]["to"] == existente.email

    # 3.2: HTML + texto puro, prazo explícito e orientação de ignorar.
    email = sent_emails[0]
    assert "Redefina sua senha" in email["assunto"]
    assert "30 minutos" in email["corpo"]
    assert "ignore este e-mail" in email["corpo"]
    assert "/redefinir-senha?token=" in email["corpo"]
    assert "Pollo" in (email["html"] or "")
    assert "/redefinir-senha?token=" in (email["html"] or "")
    assert email["html"] is not None  # multipart: HTML presente


def test_resposta_identica_quota_atingida_nao_muda_mensagem(
    client, app, make_user, sent_emails, monkeypatch
):
    from loja.services.password_reset import PUBLIC_MESSAGE as MSG

    user = make_user()
    monkeypatch.setitem(app.config, "PASSWORD_RESET_EMAIL_LIMIT", 1)

    primeiro = client.post(
        "/esqueci-senha", data={"email": user.email}, follow_redirects=True
    )
    assert MSG in primeiro.get_data(as_text=True)
    assert len(sent_emails) == 1

    segundo = client.post(
        "/esqueci-senha", data={"email": user.email}, follow_redirects=True
    )
    assert segundo.status_code == 200
    assert MSG in segundo.get_data(as_text=True)
    assert _FLASH_RE.search(segundo.get_data(as_text=True)).group(1) == MSG
    # Bloqueado: nenhum e-mail novo.
    assert len(sent_emails) == 1


# ── 4. Rate limit por IP e por conta ─────────────────────────────────────────

def test_rate_limit_por_ip_isola_por_endereco(
    client, app, make_user, sent_emails, monkeypatch
):
    from loja.services.password_reset import PUBLIC_MESSAGE as MSG

    user = make_user()
    monkeypatch.setitem(app.config, "PASSWORD_RESET_IP_LIMIT", 2)
    ip_a = {"X-Forwarded-For": "203.0.113.10"}
    ip_b = {"X-Forwarded-For": "203.0.113.11"}

    for _ in range(2):
        _solicitar(client, user.email, headers=ip_a)
    assert len(sent_emails) == 2

    bloqueado = _solicitar(client, user.email, headers=ip_a)
    assert bloqueado.status_code == 302
    assert len(sent_emails) == 2  # 3ª tentativa do mesmo IP não envia

    pagina = client.post(
        "/esqueci-senha",
        data={"email": user.email},
        headers=ip_a,
        follow_redirects=True,
    )
    assert _FLASH_RE.search(pagina.get_data(as_text=True)).group(1) == MSG

    # Outro IP, mesma conta: elegível (limite de e-mail alto por padrão).
    _solicitar(client, user.email, headers=ip_b)
    assert len(sent_emails) == 3


def test_rate_limit_por_conta_isola_por_email(
    client, app, make_user, sent_emails, monkeypatch
):
    from loja.services.password_reset import PUBLIC_MESSAGE as MSG

    user_a = make_user()
    user_b = make_user()
    monkeypatch.setitem(app.config, "PASSWORD_RESET_EMAIL_LIMIT", 1)

    _solicitar(client, user_a.email)
    assert len(sent_emails) == 1

    _solicitar(client, user_a.email)  # limite da conta A atingido
    assert len(sent_emails) == 1

    _solicitar(client, user_b.email)  # conta B não é afetada
    assert len(sent_emails) == 2

    pagina = client.post(
        "/esqueci-senha", data={"email": user_a.email}, follow_redirects=True
    )
    assert _FLASH_RE.search(pagina.get_data(as_text=True)).group(1) == MSG


# ── 4. Ciclo de vida do token ─────────────────────────────────────────────────

def test_token_valido_redefine_senha_e_nova_autentica(
    client, make_user, sent_emails
):
    from loja import db
    from loja.modules.models import User

    user = make_user()
    _solicitar(client, user.email)
    token = _token_from(sent_emails)

    resp = _redefinir(client, token, "NovaSenha456")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login")
    pagina = client.get(resp.headers["Location"], follow_redirects=True)
    assert "Senha redefinida com sucesso" in pagina.get_data(as_text=True)

    # Senha antiga parou de funcionar; nova autentica.
    antiga = client.post(
        "/login",
        data={"email": user.email, "password": "senha123"},
        follow_redirects=True,
    )
    assert "Credenciais inválidas" in antiga.get_data(as_text=True)

    nova = client.post(
        "/login",
        data={"email": user.email, "password": "NovaSenha456"},
        follow_redirects=False,
    )
    assert nova.status_code == 302

    db.session.expire_all()
    atual = User.query.filter_by(email=user.email).one()
    assert atual.check_password("NovaSenha456")
    assert not atual.check_password("senha123")


def test_token_invalido_rejeitado_com_opcao_de_novo_link(client, sent_emails):
    resp = _redefinir(client, "token-que-nao-existe-abcdefghij", "NovaSenha456")
    assert resp.status_code == 200
    pagina = resp.get_data(as_text=True)
    assert "Link inválido" in pagina
    assert "Solicitar novo link" in pagina
    assert "/esqueci-senha" in pagina


def test_token_expirado_rejeitado_com_mensagem_clara(
    client, db, make_user, sent_emails
):
    import hashlib
    import secrets as pysecrets

    from loja.modules.models import PasswordResetToken, User, utcnow

    user = make_user()
    raw = pysecrets.token_urlsafe(32)
    agora = utcnow()
    db.session.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            created_at=agora - timedelta(minutes=31),
            expires_at=agora - timedelta(minutes=1),
        )
    )
    db.session.commit()

    resp = _redefinir(client, raw, "NovaSenha456")
    assert resp.status_code == 200
    pagina = resp.get_data(as_text=True)
    assert "expirou" in pagina
    assert "Solicitar novo link" in pagina

    # Não foi trocada.
    db.session.expire_all()
    assert User.query.filter_by(email=user.email).one().check_password(
        "senha123"
    )


def test_token_ja_utilizado_rejeitado(client, make_user, sent_emails):
    user = make_user()
    _solicitar(client, user.email)
    token = _token_from(sent_emails)

    assert _redefinir(client, token, "NovaSenha456").status_code == 302

    segunda = _redefinir(client, token, "OutraSenha789")
    assert segunda.status_code == 200
    assert "já foi utilizado" in segunda.get_data(as_text=True)
    assert "Solicitar novo link" in segunda.get_data(as_text=True)


def test_nova_solicitacao_substitui_token_anterior(client, make_user, sent_emails):
    from loja import db
    from loja.modules.models import PasswordResetToken

    user = make_user()
    _solicitar(client, user.email)
    token_antigo = _token_from(sent_emails)
    _solicitar(client, user.email)
    token_novo = _token_from(sent_emails)

    assert token_antigo != token_novo

    # Token substituído foi revogado (apenas 1 ativo por usuário).
    assert (
        PasswordResetToken.query.filter_by(
            user_id=user.id, used_at=None
        ).count()
        == 1
    )

    rejeitado = _redefinir(client, token_antigo, "NovaSenha456")
    assert rejeitado.status_code == 200
    assert "Link inválido" in rejeitado.get_data(as_text=True)

    assert _redefinir(client, token_novo, "NovaSenha456").status_code == 302
    db.session.expire_all()


def test_get_redefinir_nao_consome_token_e_permite_post_valido(
    client, make_user, sent_emails
):
    user = make_user()
    _solicitar(client, user.email)
    token = _token_from(sent_emails)

    pagina = client.get(f"/redefinir-senha?token={token}")
    assert pagina.status_code == 200
    corpo = pagina.get_data(as_text=True)
    assert 'name="token"' in corpo
    assert 'autocomplete="new-password"' in corpo

    # GET não consumiu: o POST seguinte com o mesmo token funciona.
    assert _redefinir(client, token, "NovaSenha456").status_code == 302


# ── 4. CSRF, confirmação e política de senha ─────────────────────────────────

def test_post_sem_csrf_e_rejeitado(client, app, make_user, sent_emails, monkeypatch):
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", True)
    user = make_user()

    sem_csrf = client.post("/esqueci-senha", data={"email": user.email})
    assert sem_csrf.status_code == 400

    sem_csrf_redefinir = client.post(
        "/redefinir-senha",
        data={"token": "abc", "password": "x", "confirm": "x"},
    )
    assert sem_csrf_redefinir.status_code == 400


def test_confirmacao_de_senha_divergente_rejeitada(client, make_user, sent_emails):
    user = make_user()
    _solicitar(client, user.email)
    token = _token_from(sent_emails)

    resp = _redefinir(client, token, "NovaSenha456", confirm="OutraSenha789")
    assert resp.status_code == 200
    assert "As senhas não coincidem" in resp.get_data(as_text=True)


def test_senha_vazia_rejeitada_com_mensagem_clara(
    client, make_user, sent_emails
):
    user = make_user()
    _solicitar(client, user.email)
    token = _token_from(sent_emails)

    resp = _redefinir(client, token, "", confirm="")
    assert resp.status_code == 200
    assert "Todos os campos são obrigatórios" in resp.get_data(as_text=True)


def test_nova_senha_igual_a_atual_rejeitada_e_nao_consomem_token(
    client, make_user, sent_emails
):
    user = make_user()
    _solicitar(client, user.email)
    token = _token_from(sent_emails)

    resp = _redefinir(client, token, "senha123")
    assert resp.status_code == 200
    assert "não pode ser igual à senha atual" in resp.get_data(as_text=True)

    # Token não foi consumido: nova tentativa com outra senha funciona.
    assert _redefinir(client, token, "NovaSenha456").status_code == 302


# ── 4. Sessões e "lembrar-me" ─────────────────────────────────────────────────

def test_sessoes_e_remember_invalidados_apos_troca(
    client, app, make_user, sent_emails
):
    user = make_user()
    login = client.post(
        "/login",
        data={"email": user.email, "password": "senha123", "remember": "1"},
    )
    assert login.status_code == 302
    _limpar_usuario_em_g()
    assert client.get("/conta").status_code == 200

    cookie = client.get_cookie("remember_token")
    assert cookie is not None

    _solicitar(client, user.email)
    token = _token_from(sent_emails)
    troca = _redefinir(client, token, "NovaSenha456")
    assert troca.status_code == 302

    # Sessão antiga (cookie atual) → fora.
    _limpar_usuario_em_g()
    sessao_antiga = client.get("/conta")
    assert sessao_antiga.status_code == 302
    assert "/login" in sessao_antiga.headers["Location"]

    # Cookie "manter conectado" antigo em cliente novo → fora.
    cliente_novo = app.test_client()
    cliente_novo.set_cookie("remember_token", cookie.value)
    _limpar_usuario_em_g()
    remember_antigo = cliente_novo.get("/conta")
    assert remember_antigo.status_code == 302
    assert "/login" in remember_antigo.headers["Location"]

    # Nova senha autentica em cliente limpo.
    nova = cliente_novo.post(
        "/login",
        data={"email": user.email, "password": "NovaSenha456"},
    )
    assert nova.status_code == 302
    _limpar_usuario_em_g()
    assert cliente_novo.get("/conta").status_code == 200


# ── 4. Corrida: dois POSTs simultâneos ────────────────────────────────────────

def test_posts_simultaneos_apenas_um_sucede(app, db, make_user, sent_emails):
    from loja.modules.models import User
    from loja.services.password_reset import reset_password_with_token

    user = make_user()
    cliente = app.test_client()
    cliente.post("/esqueci-senha", data={"email": user.email})
    token = _token_from(sent_emails)

    resultados = []
    barreira = threading.Barrier(2)

    def _tentar():
        with app.app_context():
            barreira.wait(timeout=10)
            try:
                resultados.append(
                    reset_password_with_token(token, "NovaSenha456")
                )
            except Exception as exc:  # pragma: no cover - evidência de bug
                resultados.append(("EXCECAO", type(exc).__name__))

    threads = [
        threading.Thread(target=_tentar),
        threading.Thread(target=_tentar),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    sucessos = [r for r in resultados if r == (True, "ok")]
    perdedores = [r for r in resultados if r != (True, "ok")]
    assert len(resultados) == 2
    assert len(sucessos) == 1, resultados
    assert len(perdedores) == 1, resultados
    # O perdedor recebe resposta clara de "link inválido/já utilizado",
    # nunca exceção/erro de servidor.
    assert not any(r[0] == "EXCECAO" for r in resultados), resultados
    assert perdedores[0][0] is False
    assert perdedores[0][1] in ("used", "invalid")

    db.session.expire_all()
    atual = User.query.filter_by(email=user.email).one()
    assert atual.check_password("NovaSenha456")
    assert atual.auth_version == 2


# ── 4. Falhas e configuração ──────────────────────────────────────────────────

def test_falha_no_envio_de_email_nao_altera_resposta_publica(
    client, app, make_user, monkeypatch
):
    from loja.services.password_reset import PUBLIC_MESSAGE as MSG

    user = make_user()

    def _explode(*args, **kwargs):
        raise RuntimeError("smtp fora do ar")

    monkeypatch.setattr("loja.services.password_reset.enviar_email", _explode)

    direto = _solicitar(client, user.email)
    assert direto.status_code == 302

    pagina = client.post(
        "/esqueci-senha", data={"email": user.email}, follow_redirects=True
    )
    assert pagina.status_code == 200
    texto = pagina.get_data(as_text=True)
    assert _FLASH_RE.search(texto).group(1) == MSG


def test_falha_na_troca_reverte_consumo_do_token(
    client, db, make_user, sent_emails, monkeypatch
):
    """Compromisso 2.4/3.4: falha parcial → rollback, token não consumido."""
    from loja.modules.models import User
    from loja.services.password_reset import (
        reset_password_with_token,
        validate_reset_token,
    )

    user = make_user()
    _solicitar(client, user.email)
    token = _token_from(sent_emails)

    original = User.set_password
    estado = {"falhou": False}

    def _quebra(self, senha):
        if not estado["falhou"]:
            estado["falhou"] = True
            raise RuntimeError("falha ao gravar")
        original(self, senha)

    monkeypatch.setattr(User, "set_password", _quebra)
    ok, code = reset_password_with_token(token, "NovaSenha456")
    assert (ok, code) == (False, "invalid")

    assert validate_reset_token(token) == "ok"  # token segue ativo

    # Próxima tentativa (falha já consumida) conclui a troca.
    assert reset_password_with_token(token, "NovaSenha456") == (True, "ok")


def test_link_usa_app_public_url_e_nunca_host_forjado(
    client, make_user, sent_emails
):
    user = make_user()
    resp = client.post(
        "/esqueci-senha",
        data={"email": user.email},
        headers={"Host": "evil.example"},
    )
    assert resp.status_code == 302

    corpo = sent_emails[0]["corpo"] + (sent_emails[0]["html"] or "")
    assert "https://www.pollomodas.test/redefinir-senha?token=" in corpo
    assert "evil.example" not in corpo


def test_pagina_redefinir_envia_headers_no_referrer_e_no_store(
    client, make_user, sent_emails
):
    user = make_user()
    _solicitar(client, user.email)
    token = _token_from(sent_emails)

    get = client.get(f"/redefinir-senha?token={token}")
    assert get.headers.get("Referrer-Policy") == "no-referrer"
    assert "no-store" in get.headers.get("Cache-Control", "")

    falha = _redefinir(client, "token-invalido-zzzz", "NovaSenha456")
    assert falha.headers.get("Referrer-Policy") == "no-referrer"
    assert "no-store" in falha.headers.get("Cache-Control", "")


def test_login_exibe_link_esqueci_minha_senha(client):
    pagina = client.get("/login")
    assert pagina.status_code == 200
    assert "/esqueci-senha" in pagina.get_data(as_text=True)
    assert "Esqueci minha senha" in pagina.get_data(as_text=True)


def test_sanitizador_de_log_redige_token_da_url(client, make_user, sent_emails):
    """2.7: token nunca aparece no access log (mesma defesa do OAuth)."""
    from loja import _sanitize_oauth_access_path, _sanitize_access_requestline

    user = make_user()
    _solicitar(client, user.email)
    token = _token_from(sent_emails)

    url = f"/redefinir-senha?token={token}"
    assert _sanitize_oauth_access_path(url) == "/redefinir-senha"
    linha = f"GET {url} HTTP/1.1"
    sanitizada = _sanitize_access_requestline(linha)
    assert token not in sanitizada
    assert sanitizada == "GET /redefinir-senha HTTP/1.1"
