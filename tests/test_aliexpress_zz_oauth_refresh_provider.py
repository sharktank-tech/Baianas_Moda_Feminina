"""Fase 8.0.9 — provider real de refresh OAuth, zero rede real.

Prova:
- default continua hard-off sem flag;
- somente ALIEXPRESS_OAUTH_REFRESH_ENABLED=1 seleciona provider real;
- construir provider/factory não faz HTTP;
- endpoint exato /rest/auth/token/refresh e API path /auth/token/refresh;
- request usa app_key/refresh_token/timestamp/sign_method/sign;
- assinatura é Open Platform e nunca expõe app_secret;
- resposta válida normaliza access/refresh token + expirações;
- erro conhecido de refresh vira Rejected;
- erro desconhecido/transporte/payload inválido vira Unavailable;
- exceptions/repr nunca vazam token/secret.
"""

import json
import socket
import uuid

import pytest

from loja.integrations.aliexpress.oauth_refresh import (
    AliExpressOAuthRefreshRejected,
    AliExpressOAuthRefreshRequest,
    AliExpressOAuthRefreshUnavailable,
    HardOffOAuthRefresher,
    OAUTH_REFRESH_ENABLE_ENV,
    get_default_oauth_refresher,
    is_real_oauth_refresh_enabled,
)
from loja.integrations.aliexpress.oauth_refresh_provider import (
    AliExpressOpenPlatformOAuthRefresher,
    OAUTH_REFRESH_API_PATH,
)
from loja.integrations.aliexpress.oauth import sign_open_platform_request
from loja.integrations.aliexpress.transport import TransportError


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _reject(*args, **kwargs):
        raise AssertionError("rede real proibida nos testes da Fase 8.0.9")

    monkeypatch.setattr(socket.socket, "connect", _reject)
    monkeypatch.setattr(socket, "create_connection", _reject)


class FakeOAuthTransport:
    def __init__(self, body=None, exc=None):
        self.body = body
        self.exc = exc
        self.calls = []

    def post(self, url, params):
        self.calls.append((url, dict(params)))
        if self.exc is not None:
            raise self.exc
        return self.body


def _request(refresh_token="REFRESH_SECRET_809"):
    return AliExpressOAuthRefreshRequest(
        supplier_id=uuid.uuid4(),
        seller_id="SELLER_809",
        refresh_token=refresh_token,
    )


def test_default_fail_closed(monkeypatch):
    monkeypatch.delenv(OAUTH_REFRESH_ENABLE_ENV, raising=False)
    assert is_real_oauth_refresh_enabled() is False
    assert isinstance(get_default_oauth_refresher(), HardOffOAuthRefresher)


@pytest.mark.parametrize("value", ["", "0", "true", "TRUE", "yes", "01", " 1 "])
def test_flag_so_literal_1(monkeypatch, value):
    monkeypatch.setenv(OAUTH_REFRESH_ENABLE_ENV, value)
    assert is_real_oauth_refresh_enabled() is False
    assert isinstance(get_default_oauth_refresher(), HardOffOAuthRefresher)


def test_flag_1_constroi_provider_sem_http(monkeypatch):
    monkeypatch.setenv(OAUTH_REFRESH_ENABLE_ENV, "1")
    provider = get_default_oauth_refresher()
    assert isinstance(provider, AliExpressOpenPlatformOAuthRefresher)


def test_endpoint_assinatura_e_normalizacao(monkeypatch):
    monkeypatch.setattr(
        "loja.integrations.aliexpress.oauth_refresh_provider._timestamp_ms",
        lambda: "1788355000000",
    )
    transport = FakeOAuthTransport(
        body=json.dumps(
            {
                "access_token": "ACCESS_NEW_809",
                "refresh_token": "REFRESH_NEW_809",
                "expires_in": 2592000,
                "refresh_expires_in": 5184000,
                "seller_id": "SELLER_809",
            }
        )
    )
    provider = AliExpressOpenPlatformOAuthRefresher(
        app_key="APP_KEY_809",
        app_secret="APP_SECRET_809",
        transport=transport,
    )

    result = provider.refresh(_request())

    assert result.access_token == "ACCESS_NEW_809"
    assert result.refresh_token == "REFRESH_NEW_809"
    assert result.expires_in == 2592000
    assert result.refresh_expires_in == 5184000
    assert len(transport.calls) == 1

    url, params = transport.calls[0]
    assert url == "https://api-sg.aliexpress.com/rest/auth/token/refresh"
    assert OAUTH_REFRESH_API_PATH == "/auth/token/refresh"
    assert params["app_key"] == "APP_KEY_809"
    assert params["refresh_token"] == "REFRESH_SECRET_809"
    assert params["timestamp"] == "1788355000000"
    assert params["sign_method"] == "sha256"
    assert "app_secret" not in params
    expected = sign_open_platform_request(
        OAUTH_REFRESH_API_PATH,
        {k: v for k, v in params.items() if k != "sign"},
        "APP_SECRET_809",
    )
    assert params["sign"] == expected


def test_refresh_rejeitado_codigo_conhecido():
    transport = FakeOAuthTransport(
        body=json.dumps(
            {
                "error_response": {
                    "code": "InvalidRefreshToken",
                    "msg": "mensagem provider não deve vazar",
                }
            }
        )
    )
    provider = AliExpressOpenPlatformOAuthRefresher(
        app_key="APP_KEY_809",
        app_secret="APP_SECRET_809",
        transport=transport,
    )
    with pytest.raises(AliExpressOAuthRefreshRejected) as err:
        provider.refresh(_request())
    message = str(err.value)
    assert "REFRESH_SECRET_809" not in message
    assert "APP_SECRET_809" not in message
    assert "mensagem provider" not in message


def test_erro_oauth_desconhecido_e_indisponivel():
    transport = FakeOAuthTransport(
        body=json.dumps({"error_response": {"code": "401", "msg": "raw-sensitive"}})
    )
    provider = AliExpressOpenPlatformOAuthRefresher(
        app_key="APP_KEY_809",
        app_secret="APP_SECRET_809",
        transport=transport,
    )
    with pytest.raises(AliExpressOAuthRefreshUnavailable) as err:
        provider.refresh(_request())
    assert "raw-sensitive" not in str(err.value)


def test_transporte_indisponivel_sanitizado():
    transport = FakeOAuthTransport(
        exc=TransportError("url=https://x token=REFRESH_SECRET_809 secret=APP_SECRET_809")
    )
    provider = AliExpressOpenPlatformOAuthRefresher(
        app_key="APP_KEY_809",
        app_secret="APP_SECRET_809",
        transport=transport,
    )
    with pytest.raises(AliExpressOAuthRefreshUnavailable) as err:
        provider.refresh(_request())
    message = str(err.value)
    assert "REFRESH_SECRET_809" not in message
    assert "APP_SECRET_809" not in message
    assert "https://" not in message


@pytest.mark.parametrize("body", ["", "not-json", "[]", "null"])
def test_payload_invalido_indisponivel(body):
    provider = AliExpressOpenPlatformOAuthRefresher(
        app_key="APP_KEY_809",
        app_secret="APP_SECRET_809",
        transport=FakeOAuthTransport(body=body),
    )
    with pytest.raises(AliExpressOAuthRefreshUnavailable):
        provider.refresh(_request())


def test_config_ausente_fail_closed(monkeypatch):
    monkeypatch.delenv("ALIEXPRESS_APP_KEY", raising=False)
    monkeypatch.delenv("ALIEXPRESS_APP_SECRET", raising=False)
    provider = AliExpressOpenPlatformOAuthRefresher(
        transport=FakeOAuthTransport(body="{}")
    )
    with pytest.raises(AliExpressOAuthRefreshUnavailable):
        provider.refresh(_request())
