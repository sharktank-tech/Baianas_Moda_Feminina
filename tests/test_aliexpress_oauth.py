"""Testes offline do fluxo OAuth AliExpress (sem chamadas de rede reais)."""

from __future__ import annotations

import hashlib
import hmac
import time
from types import SimpleNamespace

import pytest
from flask import Blueprint, Flask, request

from loja.integrations.aliexpress import AliExpressSearchResult
from loja.integrations.aliexpress.errors import AliExpressAPIError, AliExpressAuthError
from loja.integrations.aliexpress.oauth import (
    DEFAULT_OAUTH_AUTHORIZE_URL,
    DEFAULT_OAUTH_API_PATH,
    AliExpressOAuthToken,
    build_authorization_url,
    exchange_authorization_code,
    sign_open_platform_request,
)
from loja.integrations.aliexpress.routes import STATE_KEY, aliexpress_blueprint


# --------------------------------------------------------------------------
# Signer Open Platform (HMAC-SHA256)
# --------------------------------------------------------------------------
def test_sign_open_platform_anchored_on_stdlib():
    params = {
        "app_key": "k",
        "code": "C",
        "sign_method": "sha256",
        "timestamp": "2020-01-01 00:00:00",
    }
    expected = hmac.new(
        b"secret",
        (DEFAULT_OAUTH_API_PATH + "".join(f"{k}{params[k]}" for k in sorted(params))).encode(),
        hashlib.sha256,
    ).hexdigest().upper()
    assert sign_open_platform_request(DEFAULT_OAUTH_API_PATH, params, "secret") == expected


def test_sign_open_platform_path_included():
    params = {"app_key": "k", "code": "C"}
    with_path = sign_open_platform_request("/auth/token/create", params, "secret")
    without_path = sign_open_platform_request("/other/path", params, "secret")
    assert with_path != without_path


def test_sign_open_platform_excludes_sign():
    params = {"app_key": "k", "code": "C", "sign": "INJECTED"}
    with_sign = sign_open_platform_request(DEFAULT_OAUTH_API_PATH, params, "secret")
    without = sign_open_platform_request(
        DEFAULT_OAUTH_API_PATH, {"app_key": "k", "code": "C"}, "secret"
    )
    assert with_sign == without


def test_sign_open_platform_distinct_from_top_hmac():
    # Garante que não confundimos sha256 (Open Platform) com hmac-md5 (TOP).
    params = {"app_key": "k", "code": "C"}
    op = sign_open_platform_request(DEFAULT_OAUTH_API_PATH, params, "secret")
    assert len(op) == 64  # SHA-256 -> 64 hex; TOP HMAC-MD5 -> 32 hex


# --------------------------------------------------------------------------
# Authorization URL
# --------------------------------------------------------------------------
def test_authorization_url_contains_params():
    url = build_authorization_url(
        client_id="mykey", redirect_uri="https://x.com/cb", state="st8"
    )
    assert url.startswith(DEFAULT_OAUTH_AUTHORIZE_URL + "?")
    assert "response_type=code" in url
    assert "client_id=mykey" in url
    assert "state=st8" in url
    assert "force_auth=true" in url
    assert "redirect_uri=" in url


def test_authorization_url_encodes_redirect_uri():
    url = build_authorization_url(
        client_id="k",
        redirect_uri="https://www.pollomodas.com.br/integrations/aliexpress/callback",
        state="st",
    )
    # O redirect_uri deve estar URL-encoded (não conter '://' cru nem espaços).
    assert "https%3A%2F%2F" in url
    assert "://" not in url.split("?")[1]


# --------------------------------------------------------------------------
# Troca de code (transporte falso)
# --------------------------------------------------------------------------
class _FakeOAuthTransport:
    def __init__(self, body: str, raise_exc: type[Exception] | None = None):
        self.body = body
        self.raise_exc = raise_exc
        self.last_url = None
        self.last_params = None

    def post(self, url: str, params: dict) -> str:
        self.last_url = url
        self.last_params = dict(params)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.body


def test_exchange_success():
    body = (
        '{"access_token":"AT","refresh_token":"RT","expires_in":604800,'
        '"refresh_expires_in":2592000,"seller_id":"S1","user_id":"U1",'
        '"account_platform":"AE"}'
    )
    tok = exchange_authorization_code("k", "s", "CODE", transport=_FakeOAuthTransport(body))
    assert isinstance(tok, AliExpressOAuthToken)
    assert tok.access_token == "AT"
    assert tok.refresh_token == "RT"
    assert tok.expires_in == 604800
    assert tok.seller_id == "S1"
    assert tok.account_platform == "AE"


def test_exchange_error_raises_auth():
    body = '{"error":{"code":"40010","msg":"invalid code"},"sub_msg":"bad"}'
    with pytest.raises(AliExpressAuthError):
        exchange_authorization_code("k", "s", "CODE", transport=_FakeOAuthTransport(body))


def test_token_repr_redacts_secrets():
    tok = AliExpressOAuthToken(access_token="AT", refresh_token="RT", seller_id="S1")
    r = repr(tok)
    assert "AT" not in r
    assert "RT" not in r
    assert "***" in r


# --------------------------------------------------------------------------
# Rotas (blueprint) com transporte falso
# --------------------------------------------------------------------------
@pytest.fixture
def app_client(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "k")
    monkeypatch.setenv("ALIEXPRESS_APP_SECRET", "s")
    monkeypatch.setenv("ALIEXPRESS_CALLBACK_URL", "https://www.pollomodas.com.br/integrations/aliexpress/callback")

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(aliexpress_blueprint)
    return app.test_client()


def _fake_token():
    return AliExpressOAuthToken(access_token="REAL_AT", refresh_token="REAL_RT", seller_id="S1", expires_in=604800)


def _fake_result(n=3):
    return AliExpressSearchResult(
        products=[SimpleNamespace(item_id=str(i)) for i in range(n)],
        total=n,
        wrapper="aliexpress.ds.text.search_response",
    )


@pytest.fixture
def auth_app():
    from flask_login import LoginManager, login_user

    app = Flask(__name__)
    app.secret_key = "test-secret"
    lm = LoginManager()
    lm.init_app(app)
    users = {}

    class _User:
        def __init__(self, admin):
            self.is_admin = admin
            self.id = "admin" if admin else "user"

        @property
        def is_authenticated(self):
            return True

        @property
        def is_active(self):
            return True

        @property
        def is_anonymous(self):
            return False

        def get_id(self):
            return self.id

    @lm.user_loader
    def load(uid):
        return users.get(uid)

    @app.route("/_test_login")
    def _test_login():
        role = request.args.get("role", "user")
        u = _User(admin=(role == "admin"))
        users[u.id] = u
        login_user(u)
        return "ok"

    main_bp = Blueprint("main", __name__)

    @main_bp.route("/login")
    def login():
        return "login"

    @main_bp.route("/")
    def home():
        return "home"

    app.register_blueprint(main_bp)
    app.register_blueprint(aliexpress_blueprint)
    return app


def test_authorize_anonymous_blocked(auth_app):
    client = auth_app.test_client()
    r = client.get("/integrations/aliexpress/authorize")
    assert r.status_code == 302
    assert "login" in r.headers["Location"]


def test_authorize_regular_user_blocked(auth_app):
    client = auth_app.test_client()
    client.get("/_test_login?role=user")
    r = client.get("/integrations/aliexpress/authorize")
    assert r.status_code == 302
    # Bloqueado: não redireciona para o OAuth da AliExpress.
    assert DEFAULT_OAUTH_AUTHORIZE_URL not in r.headers["Location"]


def test_authorize_admin_redirects_oauth(auth_app, monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "k")
    monkeypatch.setenv("ALIEXPRESS_CALLBACK_URL", "https://www.pollomodas.com.br/integrations/aliexpress/callback")
    client = auth_app.test_client()
    client.get("/_test_login?role=admin")
    r = client.get("/integrations/aliexpress/authorize")
    assert r.status_code == 302
    loc = r.headers["Location"]
    assert loc.startswith(DEFAULT_OAUTH_AUTHORIZE_URL)
    assert "state=" in loc
    assert "client_id=k" in loc


def test_callback_expired_state_rejected(auth_app, monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "k")
    monkeypatch.setenv("ALIEXPRESS_APP_SECRET", "s")
    monkeypatch.setenv("ALIEXPRESS_CALLBACK_URL", "https://x.com/cb")
    client = auth_app.test_client()
    with client.session_transaction() as sess:
        sess[STATE_KEY] = {"state": "OLD", "ts": time.time() - 900}
    r = client.get("/integrations/aliexpress/callback?code=ABC&state=OLD")
    assert r.status_code == 400


def test_callback_success_shows_no_token(app_client, monkeypatch):
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.exchange_authorization_code", lambda *a, **k: _fake_token()
    )
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.AliExpressClient",
        lambda *a, **k: SimpleNamespace(search_products=lambda **kw: _fake_result(3)),
    )
    state = "ST1"
    with app_client.session_transaction() as sess:
        sess[STATE_KEY] = {"state": state, "ts": time.time()}
    r = app_client.get(f"/integrations/aliexpress/callback?code=ABC123&state={state}")
    body = r.data.decode()
    assert r.status_code == 200
    assert "DS smoke: success" in body
    assert "produtos recebidos: 3" in body
    assert "REAL_AT" not in body  # token nunca exibido
    assert "REAL_RT" not in body
    assert "ABC123" not in body  # code nunca exibido


def test_callback_invalid_state_rejected(app_client):
    with app_client.session_transaction() as sess:
        sess[STATE_KEY] = {"state": "REAL", "ts": time.time()}
    r = app_client.get("/integrations/aliexpress/callback?code=ABC&state=WRONG")
    assert r.status_code == 400


def test_callback_missing_state_rejected(app_client):
    r = app_client.get("/integrations/aliexpress/callback?code=ABC")
    assert r.status_code == 400


def test_callback_state_replay_rejected(app_client, monkeypatch):
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.exchange_authorization_code", lambda *a, **k: _fake_token()
    )
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.AliExpressClient",
        lambda *a, **k: SimpleNamespace(search_products=lambda **kw: _fake_result(1)),
    )
    state = "ST_REPLAY"
    with app_client.session_transaction() as sess:
        sess[STATE_KEY] = {"state": state, "ts": time.time()}
    r1 = app_client.get(f"/integrations/aliexpress/callback?code=ABC&state={state}")
    assert r1.status_code == 200
    # Replay do mesmo state (já consumido) -> rejeitado.
    r2 = app_client.get(f"/integrations/aliexpress/callback?code=ABC&state={state}")
    assert r2.status_code == 400


def test_callback_ds_smoke_error_sanitized(app_client, monkeypatch):
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.exchange_authorization_code", lambda *a, **k: _fake_token()
    )

    class _FailClient:
        def __init__(self, *a, **k):
            pass

        def search_products(self, **kw):
            raise AliExpressAPIError("bad request", code=20020, sub_code="x", raw={"request_id": "RID1"})

    monkeypatch.setattr("loja.integrations.aliexpress.routes.AliExpressClient", _FailClient)
    state = "ST_ERR"
    with app_client.session_transaction() as sess:
        sess[STATE_KEY] = {"state": state, "ts": time.time()}
    r = app_client.get(f"/integrations/aliexpress/callback?code=ABC&state={state}")
    body = r.data.decode()
    assert r.status_code == 200
    assert "DS smoke: failure" in body
    assert "code: 20020" in body
    assert "RID1" in body  # request_id pode ser exibido (não é secreto)
    assert "REAL_AT" not in body  # token nunca exibido
