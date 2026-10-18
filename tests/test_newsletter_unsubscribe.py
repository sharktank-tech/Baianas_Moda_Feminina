"""Opt-out da newsletter: cancelamento, repetição, token, supressão, reinscrição."""
import uuid

import pytest

from loja import db
from loja.modules import newsletter as newsletter_mod
from loja.modules.models import NewsletterSubscriber
from loja.modules.newsletter import (
    generate_unsubscribe_token,
    is_marketable,
    is_subscription_suppressed,
    reactivate_by_token,
    subscribe_newsletter,
    unsubscribe_by_token,
    verify_unsubscribe_token,
)
from loja.utils import utcnow
from tests.conftest import unique_email


@pytest.fixture(autouse=True)
def _app_ctx(app):
    with app.app_context():
        yield


@pytest.fixture(autouse=True)
def _no_real_email(monkeypatch):
    sent = []

    def _capture(email, subject, body, *args, **kwargs):
        sent.append((email, subject, body))
        return True

    monkeypatch.setattr(newsletter_mod, "enviar_email", _capture)
    return sent


@pytest.fixture()
def subscriber_ativo(_no_real_email):
    email = unique_email("news")
    result = subscribe_newsletter(email)
    assert result.success and result.created
    return result.subscriber


def test_cancelamento_por_post(subscriber_ativo, client, _no_real_email):
    token = generate_unsubscribe_token(subscriber_ativo)
    # GET: apenas confirmação, sem alterar estado.
    resp = client.get(f"/newsletter/cancelar/{token}")
    assert resp.status_code == 200
    assert b"cancelar" in resp.data.lower()
    db.session.refresh(subscriber_ativo)
    assert subscriber_ativo.unsubscribed_at is None

    resp = client.post(
        f"/newsletter/cancelar/{token}",
        data={"acao": "cancelar"},
    )
    assert resp.status_code == 200
    db.session.refresh(subscriber_ativo)
    assert subscriber_ativo.unsubscribed_at is not None
    assert is_subscription_suppressed(subscriber_ativo.email)
    assert not is_marketable(subscriber_ativo.email)
    assert "cancelada" in resp.data.decode("utf-8").lower()


def test_cancelamento_idempotente(subscriber_ativo, client):
    token = generate_unsubscribe_token(subscriber_ativo)
    client.post(f"/newsletter/cancelar/{token}", data={"acao": "cancelar"})
    first = subscriber_ativo.unsubscribed_at
    resp = client.post(f"/newsletter/cancelar/{token}", data={"acao": "cancelar"})
    assert resp.status_code == 200
    db.session.refresh(subscriber_ativo)
    assert subscriber_ativo.unsubscribed_at == first


def test_cadastro_repetido_nao_reativa(subscriber_ativo, client, _no_real_email):
    token = generate_unsubscribe_token(subscriber_ativo)
    client.post(f"/newsletter/cancelar/{token}", data={"acao": "cancelar"})
    sent_before = len(_no_real_email)

    result = subscribe_newsletter(subscriber_ativo.email)
    assert result.success is False
    assert "cancelado" in result.message.lower()
    assert result.created is False
    db.session.refresh(subscriber_ativo)
    assert subscriber_ativo.unsubscribed_at is not None
    assert len(_no_real_email) == sent_before  # sem e-mail de boas-vindas


def test_token_invalido(client, subscriber_ativo):
    resp = client.get("/newsletter/cancelar/token-invalido")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8").lower()
    assert "inválido" in body
    assert subscriber_ativo.email not in body  # sem vazar e-mail

    resp = client.post(
        "/newsletter/cancelar/abc.def", data={"acao": "cancelar"}
    )
    assert resp.status_code == 200
    assert "inválido" in resp.data.decode("utf-8").lower()
    db.session.refresh(subscriber_ativo)
    assert subscriber_ativo.unsubscribed_at is None


def test_token_de_outro_assinante_nao_valida(subscriber_ativo, client):
    outro = subscribe_newsletter(unique_email("news")).subscriber
    token_outro = generate_unsubscribe_token(outro)
    # troca do sufixo MAC por outro id: id de `outro` + MAC de `subscriber_ativo`
    id_outro, mac = token_outro.split(".")
    mac_sub = generate_unsubscribe_token(subscriber_ativo).split(".")[1]
    resp = client.get(f"/newsletter/cancelar/{id_outro}.{mac_sub}")
    assert "inválido" in resp.data.decode("utf-8").lower()


def test_supressão_bloqueia_welcome(subscriber_ativo, client, _no_real_email):
    token = generate_unsubscribe_token(subscriber_ativo)
    client.post(f"/newsletter/cancelar/{token}", data={"acao": "cancelar"})
    sent_before = len(_no_real_email)

    # Envio promocional direto (futuro código de campanha) via helper.
    assert not is_marketable(subscriber_ativo.email)
    assert is_marketable(unique_email("nunca-cadastrado"))

    # subscribe com send_welcome_email explícito também não envia.
    subscribe_newsletter(subscriber_ativo.email, send_welcome_email=True)
    assert len(_no_real_email) == sent_before


def test_reinscrição_explícita(subscriber_ativo, client):
    token = generate_unsubscribe_token(subscriber_ativo)
    client.post(f"/newsletter/cancelar/{token}", data={"acao": "cancelar"})
    db.session.refresh(subscriber_ativo)
    assert subscriber_ativo.unsubscribed_at is not None

    resp = client.post(
        f"/newsletter/cancelar/{token}", data={"acao": "reativar"}
    )
    assert resp.status_code == 200
    db.session.refresh(subscriber_ativo)
    assert subscriber_ativo.unsubscribed_at is None
    assert subscriber_ativo.resubscribed_at is not None
    assert is_marketable(subscriber_ativo.email)

    # após reativação, cadastro repetido volta ao caminho "já cadastrado".
    result = subscribe_newsletter(subscriber_ativo.email)
    assert result.success is True
    assert "já está cadastrado" in result.message.lower()


def test_mesmo_token_cancelar_reativar_cancelar(client, subscriber_ativo):
    """Reutilização do mesmo token: cancelar → reativar → cancelar de novo."""
    token = generate_unsubscribe_token(subscriber_ativo)

    client.post(f"/newsletter/cancelar/{token}", data={"acao": "cancelar"})
    db.session.refresh(subscriber_ativo)
    assert subscriber_ativo.unsubscribed_at is not None

    client.post(f"/newsletter/cancelar/{token}", data={"acao": "reativar"})
    db.session.refresh(subscriber_ativo)
    assert subscriber_ativo.unsubscribed_at is None
    assert subscriber_ativo.resubscribed_at is not None

    client.post(f"/newsletter/cancelar/{token}", data={"acao": "cancelar"})
    db.session.refresh(subscriber_ativo)
    assert subscriber_ativo.unsubscribed_at is not None
    assert is_subscription_suppressed(subscriber_ativo.email)
    assert not is_marketable(subscriber_ativo.email)


def test_token_nao_contem_email(subscriber_ativo):
    token = generate_unsubscribe_token(subscriber_ativo)
    assert "@" not in token
    assert subscriber_ativo.email not in token
    assert verify_unsubscribe_token(token) is not None


def test_token_expira_com_secret_rotacionada(app, subscriber_ativo):
    token = generate_unsubscribe_token(subscriber_ativo)
    original = app.config["SECRET_KEY"]
    try:
        app.config["SECRET_KEY"] = "outra-secret-rotacionada"
        assert verify_unsubscribe_token(token) is None
    finally:
        app.config["SECRET_KEY"] = original
    assert verify_unsubscribe_token(token) is not None


def test_welcome_inclui_link_cancelamento(client, _no_real_email):
    # Inscrição via rota real: contexto de request permite URL externa.
    email = unique_email("news")
    resp = client.post("/newsletter", data={"email": email})
    assert resp.status_code == 302
    assert _no_real_email, "boas-vindas deveria ter sido enviada"
    to, subject, body = _no_real_email[-1]
    assert to == email
    assert "cancelar" in body.lower()
    assert "/newsletter/cancelar/" in body


def test_id_desconhecido_no_token(subscriber_ativo):
    fake_id = uuid.uuid4()
    token = f"{fake_id.hex}.{'a' * 32}"
    assert verify_unsubscribe_token(token) is None
    assert unsubscribe_by_token(token).success is False
    assert reactivate_by_token(token).success is False


def test_fluxo_comunicacoes_transacionais_separadas():
    """A supressão vive só no módulo da newsletter (envios promocionais).

    O módulo de recuperação de checkout (transacional/opera cional) não
    importa nem consulta a newsletter.
    """
    import loja.services.checkout_recovery as recovery

    source = open(recovery.__file__, encoding="utf-8").read()
    assert "newsletter" not in source
    assert "is_marketable" not in source
    assert "is_subscription_suppressed" not in source

def test_acao_invalida_nao_alterada(client, subscriber_ativo, _no_real_email):
    """POST com acao desconhecida deve falhar sem alterar o banco."""
    import uuid
    from loja.modules.models import NewsletterSubscriber
    from loja.modules.newsletter import generate_unsubscribe_token, is_subscription_suppressed
    
    # Setup: create a subscriber and generate token
    token = generate_unsubscribe_token(subscriber_ativo)
    
    # Capture DB state before
    from sqlalchemy.orm import Session
    before_unsub = subscriber_ativo.unsubscribed_at
    
    # POST with invalid acao
    resp = client.post(
        f"/newsletter/cancelar/{token}",
        data={"acao": "spam"},
    )
    assert resp.status_code == 200
    
    # Check DB was NOT altered
    from loja import db as _db
    _db.session.refresh(subscriber_ativo)
    assert subscriber_ativo.unsubscribed_at == before_unsub
    
    # Check response has error message
    body = resp.data.decode("utf-8").lower()
    assert "ação desconhecida" in body or "invalida" in body


def test_acao_ausente_nao_altera(client, subscriber_ativo, _no_real_email):
    """POST sem campo acao deve falhar sem alterar a inscricao."""
    token = generate_unsubscribe_token(subscriber_ativo)

    before_unsub = subscriber_ativo.unsubscribed_at

    resp = client.post(
        f"/newsletter/cancelar/{token}",
        data={},
    )
    assert resp.status_code == 200

    from loja import db as _db
    _db.session.refresh(subscriber_ativo)
    assert subscriber_ativo.unsubscribed_at == before_unsub

    body = resp.data.decode("utf-8").lower()
    assert "ação desconhecida" in body or "invalida" in body


def test_acao_vazia_tratada(client, subscriber_ativo, _no_real_email):
    """POST com acao=vazio deve ser tratado como inválido."""
    token = generate_unsubscribe_token(subscriber_ativo)
    resp = client.post(
        f"/newsletter/cancelar/{token}",
        data={"acao": ""},
    )
    body = resp.data.decode("utf-8").lower()
    # Voucher vazia não é 'cancelar' nem 'reativar'
    assert "ação desconhecida" in body or "invalida" in body

def _get_csrf_token_from_form(html):
    """Extrai o valor do csrf_token do HTML da página."""
    import re
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else None


def test_csrf_sem_token(client, subscriber_ativo, app):
    """POST sem csrf_token: 400 e banco intacto com CSRF ativo."""
    original = app.config.get("WTF_CSRF_ENABLED", False)
    app.config["WTF_CSRF_ENABLED"] = True
    app.config["SECRET_KEY"] = "test-csrf-secret-key-" + str(uuid.uuid4())[:8]

    try:
        token = generate_unsubscribe_token(subscriber_ativo)
        resp = client.post(
            f"/newsletter/cancelar/{token}",
            data={"acao": "cancelar"},
        )
        assert resp.status_code == 400
        db.session.refresh(subscriber_ativo)
        assert subscriber_ativo.unsubscribed_at is None
        assert is_marketable(subscriber_ativo.email)
    finally:
        app.config["WTF_CSRF_ENABLED"] = original


def test_csrf_token_invalido(client, subscriber_ativo, app):
    """POST com csrf_token inválido: 400 e banco intacto com CSRF ativo."""
    original = app.config.get("WTF_CSRF_ENABLED", False)
    app.config["WTF_CSRF_ENABLED"] = True
    app.config["SECRET_KEY"] = "test-csrf-secret-key-" + str(uuid.uuid4())[:8]

    try:
        token = generate_unsubscribe_token(subscriber_ativo)
        resp = client.post(
            f"/newsletter/cancelar/{token}",
            data={"acao": "cancelar", "csrf_token": "token-invalido-12345"},
        )
        assert resp.status_code == 400
        db.session.refresh(subscriber_ativo)
        assert subscriber_ativo.unsubscribed_at is None
        assert is_marketable(subscriber_ativo.email)
    finally:
        app.config["WTF_CSRF_ENABLED"] = original


def test_csrf_token_valido(client, subscriber_ativo, app):
    """POST com csrf_token valido deve ser aceito e alterar o banco."""
    from loja.modules.newsletter import is_marketable

    original = app.config.get("WTF_CSRF_ENABLED", False)
    app.config["WTF_CSRF_ENABLED"] = True
    app.config["SECRET_KEY"] = "test-csrf-secret-key-" + str(uuid.uuid4())[:8]

    try:
        token = generate_unsubscribe_token(subscriber_ativo)

        # GET da pagina de confirmacao: o template inclui o csrf_token valido
        resp_get = client.get(f"/newsletter/cancelar/{token}")
        assert resp_get.status_code == 200
        csrf = _get_csrf_token_from_form(resp_get.data.decode())
        assert csrf, "csrf_token ausente no formulario"

        resp = client.post(
            f"/newsletter/cancelar/{token}",
            data={"acao": "cancelar", "csrf_token": csrf},
        )
        assert resp.status_code == 200
        assert not is_marketable(subscriber_ativo.email)
    finally:
        app.config["WTF_CSRF_ENABLED"] = original


def test_csrf_estado_banco(client, subscriber_ativo, app):
    """POSTs com CSRF ausente/inválido não alteram o banco; válido cancela."""
    original = app.config.get("WTF_CSRF_ENABLED", False)
    app.config["WTF_CSRF_ENABLED"] = True
    app.config["SECRET_KEY"] = "test-csrf-secret-key-" + str(uuid.uuid4())[:8]

    from loja.modules.newsletter import is_marketable
    try:
        token = generate_unsubscribe_token(subscriber_ativo)

        # GET estabelece sessao com csrf valido para os POSTs seguintes
        resp_get = client.get(f"/newsletter/cancelar/{token}")
        assert resp_get.status_code == 200
        csrf = _get_csrf_token_from_form(resp_get.data.decode())
        assert csrf

        resp_sem = client.post(
            f"/newsletter/cancelar/{token}",
            data={"acao": "cancelar"},
        )
        assert resp_sem.status_code == 400
        assert is_marketable(subscriber_ativo.email)

        resp_inv = client.post(
            f"/newsletter/cancelar/{token}",
            data={"acao": "cancelar", "csrf_token": "token-invalido-xyz"},
        )
        assert resp_inv.status_code == 400
        assert is_marketable(subscriber_ativo.email)

        resp_ok = client.post(
            f"/newsletter/cancelar/{token}",
            data={"acao": "cancelar", "csrf_token": csrf},
        )
        assert resp_ok.status_code == 200
        assert not is_marketable(subscriber_ativo.email)
    finally:
        app.config["WTF_CSRF_ENABLED"] = original


def test_concorrencia_objeto_stale_nao_envia(app, _no_real_email):
    """Duas sessões SQLAlchemy distintas: cancelamento confirmado na
    segunda sessão impede o envio com o objeto antigo carregado na
    primeira. Exige retorno sem envio e zero chamadas ao provedor."""
    from sqlalchemy.orm import Session

    from loja.modules.newsletter import _send_welcome_email

    email = unique_email("conc")
    result = subscribe_newsletter(email)
    assert result.success and result.created
    subscriber_id = result.subscriber.id

    with app.app_context():
        # Sessão 1 (db.session): carrega o assinante (identity map).
        sessao1_obj = db.session.get(NewsletterSubscriber, subscriber_id)
        assert sessao1_obj is not None
        assert sessao1_obj.unsubscribed_at is None
        db.session.expunge(sessao1_obj)  # objeto antigo, detached

        # Sessão 2 (realmente distinta): cancela e confirma no banco.
        with Session(db.engine) as sessao2:
            sub2 = sessao2.get(NewsletterSubscriber, subscriber_id)
            sub2.unsubscribed_at = utcnow()
            sessao2.commit()
            confirmado = sessao2.get(NewsletterSubscriber, subscriber_id)
            assert confirmado.unsubscribed_at is not None

        # Caminho real de envio com o objeto antigo (stale/detached).
        sent_before = len(_no_real_email)
        ok = _send_welcome_email(sessao1_obj)
        assert ok is False
        assert len(_no_real_email) == sent_before  # zero chamadas ao provedor

        # Estado real no banco permanece cancelado.
        fresco = db.session.get(NewsletterSubscriber, subscriber_id)
        db.session.refresh(fresco)
        assert fresco.unsubscribed_at is not None
        assert not is_marketable(email)


def test_concorrencia_contraprova_ativo_envia(app, _no_real_email):
    """Contraprova: assinante ativo passa pela recarga e o envio ocorre."""
    from loja.modules.newsletter import _send_welcome_email

    email = unique_email("conc-ok")
    result = subscribe_newsletter(email, send_welcome_email=False)
    assert result.success and result.created

    with app.app_context():
        obj = db.session.get(NewsletterSubscriber, result.subscriber.id)
        db.session.expunge(obj)  # detached, mas ativo no banco
        sent_before = len(_no_real_email)
        ok = _send_welcome_email(obj)
        assert ok is True
        assert len(_no_real_email) == sent_before + 1
