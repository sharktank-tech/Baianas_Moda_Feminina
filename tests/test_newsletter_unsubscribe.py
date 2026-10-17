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
