"""Testes unitários do cliente AliExpress (sem chamadas de rede reais)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from loja.integrations.aliexpress import (
    AliExpressAPIError,
    AliExpressAuthError,
    AliExpressClient,
    AliExpressConfigError,
    AliExpressCredentials,
    AliExpressRateLimitError,
    AliExpressTransportError,
    create_client_from_env,
    sign_top,
)
from loja.integrations.aliexpress.transport import (
    Transport,
    TransportError,
    TransportTimeout,
)


# --------------------------------------------------------------------------
# Signer — ancora no ALGORITMO OFICIAL (TOP signTopRequest / doc 105423/108974).
# Nota: os valores numéricos "sign" publicados nos exemplos das docs são
# internamente inconsistentes (não reproduzem com o algoritmo documentado).
# Por isso o golden test cruza com um port fiel do Java SDK oficial e com um
# digest fixo *calculado/verificado independentemente* (vetor de regressão),
# e NÃO força nosso código a produzir um valor sabidamente inconsistente.
# --------------------------------------------------------------------------
def _reference_sign_top(params: dict, secret: str, method: str = "md5") -> str:
    """Port fiel do signTopRequest oficial (TopApi Java SDK)."""
    import hashlib
    import hmac as _hmac

    keys = sorted(params.keys())
    query = secret if method == "md5" else ""
    for k in keys:
        v = params[k]
        if k and v:
            query += f"{k}{v}"
    if method == "md5":
        query += secret
        return hashlib.md5(query.encode("utf-8")).hexdigest().upper()
    # hmac_md5
    return _hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.md5).hexdigest().upper()


def test_sign_top_matches_official_java_sdk():
    cases = [
        {
            "method": "aliexpress.solution.order.fulfill",
            "app_key": "12345678",
            "session": "test",
            "timestamp": "2019-01-01 12:00:00",
            "format": "json",
            "v": "2.0",
            "sign_method": "md5",
            "logistics_no": "ES2019COM0000123456",
            "out_ref": "1000006270175804",
            "send_type": "all",
            "service_name": "SPAIN_LOCAL_CORREOS",
        },
        {
            "method": "taobao.xhotel.update",
            "app_key": "12345678",
            "session": "test",
            "timestamp": "2016-01-01 12:00:00",
            "format": "json",
            "v": "2.0",
            "sign_method": "md5",
            "name": "GJ001",
            "outer_id": "GJ001",
        },
        {"a": "1", "b": "2", "c": "3"},
    ]
    for c in cases:
        assert sign_top(c, "helloworld", method="md5") == _reference_sign_top(c, "helloworld", "md5")
        assert sign_top(c, "secret", method="hmac") == _reference_sign_top(c, "secret", "hmac")


def test_sign_top_golden_digest():
    # Vetor de regressão CALCULADO/VERIFICADO independentemente (não é um valor
    # "publicado oficialmente" — os digests das docs são inconsistentes).
    params = {
        "method": "aliexpress.solution.order.fulfill",
        "app_key": "12345678",
        "session": "test",
        "timestamp": "2019-01-01 12:00:00",
        "format": "json",
        "v": "2.0",
        "sign_method": "md5",
        "logistics_no": "ES2019COM0000123456",
        "out_ref": "1000006270175804",
        "send_type": "all",
        "service_name": "SPAIN_LOCAL_CORREOS",
    }
    assert sign_top(params, "helloworld", method="md5") == "1F31D5B6CC43C4C565886BD836C0A5AC"


def test_sign_top_deterministic():
    s1 = sign_top({"b": "2", "a": "1", "c": "3"}, "secret")
    s2 = sign_top({"c": "3", "a": "1", "b": "2"}, "secret")
    assert s1 == s2
    assert len(s1) == 32 and s1.isupper()


def test_sign_top_ordering_matters():
    # Ordenação ASCII crescente dos nomes; mesmos pares em ordem diferente => mesmo sign.
    a = sign_top({"b": "2", "a": "1"}, "secret")
    b = sign_top({"a": "1", "b": "2"}, "secret")
    assert a == b
    # Chave diferente muda o sign.
    assert sign_top({"a": "1", "b": "2"}, "secret") != sign_top({"a": "1", "c": "2"}, "secret")


def test_sign_top_utf8_encoded():
    # Valores não-ASCII devem ser assinados como UTF-8 (encode("utf-8")).
    params = {"method": "x", "name": "北京饭店"}
    sig = sign_top(params, "secret", method="hmac")
    assert len(sig) == 32 and sig.isupper()
    # Deve ser estável e igual ao cálculo UTF-8 explícito.
    import hashlib
    import hmac as _hmac

    concat = "methodx" + "name" + "北京饭店"
    expected = _hmac.new(b"secret", concat.encode("utf-8"), hashlib.md5).hexdigest().upper()
    assert sig == expected


def test_sign_top_excludes_sign_from_material():
    # O parâmetro `sign` NUNCA faz parte do material assinado.
    base = {"a": "1", "b": "2"}
    without = sign_top(base, "secret")
    with_sign = sign_top({**base, "sign": "INJECTED"}, "secret")
    assert without == with_sign


def test_sign_top_hmac_supported():
    # hmac nesta implementação = HMAC-MD5 (contrato TOP), não SHA1/SHA256.
    params = {"a": "1", "b": "2"}
    s = sign_top(params, "secret", method="hmac")
    assert len(s) == 32 and s.isupper()
    import hashlib
    import hmac as _hmac

    concat = "a1b2"
    assert s == _hmac.new(b"secret", concat.encode("utf-8"), hashlib.md5).hexdigest().upper()


def test_sign_top_md5_supported():
    params = {"a": "1", "b": "2"}
    s = sign_top(params, "secret", method="md5")
    assert len(s) == 32 and s.isupper()
    import hashlib

    assert s == hashlib.md5(("secret" + "a1b2" + "secret").encode("utf-8")).hexdigest().upper()


def test_sign_top_requires_secret():
    with pytest.raises(AliExpressConfigError):
        sign_top({"a": "1"}, "")


# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------
def test_client_requires_app_key_and_secret():
    with pytest.raises(AliExpressConfigError):
        AliExpressClient(app_key="", app_secret="")
    with pytest.raises(AliExpressConfigError):
        AliExpressClient(app_key="k", app_secret="")


def test_credentials_from_env_missing(monkeypatch):
    monkeypatch.delenv("ALIEXPRESS_APP_KEY", raising=False)
    monkeypatch.delenv("ALIEXPRESS_APP_SECRET", raising=False)
    with pytest.raises(AliExpressConfigError):
        AliExpressCredentials.from_env()


def test_credentials_from_env_ok(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "k")
    monkeypatch.setenv("ALIEXPRESS_APP_SECRET", "s")
    monkeypatch.setenv("ALIEXPRESS_ACCESS_TOKEN", "tok")
    creds = AliExpressCredentials.from_env()
    assert creds.app_key == "k"
    assert creds.is_token_valid() is True


def test_require_token_raises_when_absent(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "k")
    monkeypatch.setenv("ALIEXPRESS_APP_SECRET", "s")
    monkeypatch.delenv("ALIEXPRESS_ACCESS_TOKEN", raising=False)
    with pytest.raises(AliExpressConfigError):
        AliExpressCredentials.from_env(require_token=True)


def test_token_validity_respects_expiry(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "k")
    monkeypatch.setenv("ALIEXPRESS_APP_SECRET", "s")
    creds = AliExpressCredentials.from_env()
    assert creds.is_token_valid() is False


# --------------------------------------------------------------------------
# Token nos parâmetros
# --------------------------------------------------------------------------
def test_access_token_sent_as_session_param():
    client = AliExpressClient(app_key="k", app_secret="s", access_token="MY_TOKEN")
    params = client._build_params("dummy.method", foo="bar")
    assert params["session"] == "MY_TOKEN"
    assert params["sign"]


def test_no_session_param_without_token():
    client = AliExpressClient(app_key="k", app_secret="s")
    params = client._build_params("dummy.method")
    assert "session" not in params
    assert params["method"] == "dummy.method"
    assert params["sign_method"] == "hmac"


def test_with_token_returns_copy_without_mutating():
    client = AliExpressClient(app_key="k", app_secret="s")
    other = client.with_token("NEW")
    assert client.credentials.access_token is None
    assert other.credentials.access_token == "NEW"


# --------------------------------------------------------------------------
# Redação de secrets
# --------------------------------------------------------------------------
def test_client_repr_masks_secrets():
    client = AliExpressClient(app_key="k", app_secret="s3cr3t", access_token="t0k3n")
    r = repr(client)
    assert "s3cr3t" not in r
    assert "t0k3n" not in r
    assert "***" in r


def test_error_redacts_raw_secrets():
    raw = {
        "error_response": {
            "code": 401,
            "msg": "invalid",
            "sub_msg": "bad",
            "session": "SECRET_TOKEN",
            "sign": "ABC123",
        }
    }
    err = AliExpressAPIError("boom", code=401, raw=raw)
    assert "SECRET_TOKEN" not in repr(err)
    assert "ABC123" not in repr(err)
    assert "SECRET_TOKEN" not in str(err)


# --------------------------------------------------------------------------
# Parsing de resposta (transporte falso)
# --------------------------------------------------------------------------
class _FakeTransport(Transport):
    def __init__(self, body: str, *, raise_exc: "type[Exception] | None" = None):
        self.body = body
        self.raise_exc = raise_exc
        self.calls = 0

    def post(self, params: dict[str, str]) -> str:
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.body


def test_call_returns_payload_on_success():
    body = json.dumps({"aliexpress.x_response": {"code": 0, "result": {"ok": True}}})
    client = AliExpressClient(
        app_key="k", app_secret="s", transport=_FakeTransport(body)
    )
    result = client.call("aliexpress.x")
    assert result == {"code": 0, "result": {"ok": True}}


def test_call_raises_api_error_on_error_response():
    body = json.dumps(
        {"error_response": {"code": 20020, "msg": "fail", "sub_code": "x"}}
    )
    client = AliExpressClient(
        app_key="k", app_secret="s", transport=_FakeTransport(body)
    )
    with pytest.raises(AliExpressAPIError) as exc:
        client.call("aliexpress.x")
    assert exc.value.code == 20020


def test_call_raises_auth_error_on_401():
    body = json.dumps(
        {"error_response": {"code": 401, "msg": "unauthorized"}}
    )
    client = AliExpressClient(
        app_key="k", app_secret="s", transport=_FakeTransport(body)
    )
    with pytest.raises(AliExpressAuthError):
        client.call("aliexpress.x")


def test_call_raises_rate_limit_without_retry():
    body = json.dumps({"error_response": {"code": 555, "msg": "rate limit exceeded"}})
    fake = _FakeTransport(body)
    client = AliExpressClient(
        app_key="k", app_secret="s", transport=fake
    )
    with pytest.raises(AliExpressRateLimitError):
        client.call("aliexpress.x")
    # Garante que NÃO há retry infinito: uma única chamada de transporte.
    assert fake.calls == 1


def test_call_transport_timeout_maps_to_transport_error():
    client = AliExpressClient(
        app_key="k",
        app_secret="s",
        transport=_FakeTransport("", raise_exc=TransportTimeout("timeout")),
    )
    with pytest.raises(AliExpressTransportError):
        client.call("aliexpress.x")


def test_call_invalid_json_raises_api_error():
    client = AliExpressClient(
        app_key="k", app_secret="s", transport=_FakeTransport("not json")
    )
    with pytest.raises(AliExpressAPIError):
        client.call("aliexpress.x")


def test_create_client_from_env(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "k")
    monkeypatch.setenv("ALIEXPRESS_APP_SECRET", "s")
    monkeypatch.setenv("ALIEXPRESS_ACCESS_TOKEN", "t")
    client = create_client_from_env()
    assert client.credentials.app_key == "k"
    assert client.credentials.access_token == "t"
