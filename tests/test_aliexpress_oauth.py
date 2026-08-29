"""Testes offline do fluxo OAuth AliExpress (sem chamadas de rede reais)."""

from __future__ import annotations

import hashlib
import hmac
import time
from types import SimpleNamespace

import pytest
from flask import Blueprint, Flask, request

from loja.integrations.aliexpress import AliExpressSearchResult
from loja.integrations.aliexpress.errors import (
    AliExpressAPIError,
    AliExpressAuthError,
    AliExpressConfigError,
)
from loja.integrations.aliexpress.oauth import (
    DEFAULT_OAUTH_AUTHORIZE_URL,
    DEFAULT_OAUTH_API_BASE_URL,
    DEFAULT_OAUTH_API_PATH,
    AliExpressOAuthToken,
    build_authorization_url,
    exchange_authorization_code,
    normalize_oauth_token,
    sign_open_platform_request,
    _now_open_platform_timestamp,
)
from loja.integrations.aliexpress.routes import (
    STATE_KEY,
    aliexpress_blueprint,
    get_aliexpress_callback_url,
)


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
    from urllib.parse import urlsplit, parse_qs

    url = build_authorization_url(
        client_id="mykey", redirect_uri="https://x.com/cb", state="st8"
    )
    assert url.startswith(DEFAULT_OAUTH_AUTHORIZE_URL + "?")
    host = DEFAULT_OAUTH_AUTHORIZE_URL.split("://", 1)[1]
    assert host == "api-sg.aliexpress.com/oauth/authorize"
    q = parse_qs(urlsplit(url).query)
    assert q.get("response_type") == ["code"]
    assert q.get("client_id") == ["mykey"]
    assert q.get("state") == ["st8"]
    assert q.get("force_auth") == ["true"]
    assert "sp" not in q
    assert "view" not in q
    assert "redirect_uri" in q


def test_authorization_url_open_platform_shape():
    url = build_authorization_url(
        client_id="mykey",
        redirect_uri="https://www.pollomodas.com.br/integrations/aliexpress/callback",
        state="abc123",
    )
    from urllib.parse import urlsplit, parse_qs

    parts = urlsplit(url)
    assert parts.scheme == "https"
    assert parts.netloc == "api-sg.aliexpress.com"
    assert parts.path == "/oauth/authorize"
    q = parse_qs(parts.query)
    assert q.get("response_type") == ["code"]
    assert q.get("client_id") == ["mykey"]
    assert q.get("state") == ["abc123"]
    assert q.get("redirect_uri") == [
        "https://www.pollomodas.com.br/integrations/aliexpress/callback"
    ]
    assert q.get("force_auth") == ["true"]
    assert "sp" not in q
    assert "view" not in q


def test_protocol_consistency_no_hybrid_mix():
    # Impede misturar oauth.aliexpress.com com api-sg.aliexpress.com/rest.
    # Ambos os endpoints (authorize e token) devem estar no mesmo host Open
    # Platform: api-sg.aliexpress.com.
    from urllib.parse import urlsplit

    auth = urlsplit(DEFAULT_OAUTH_AUTHORIZE_URL)
    token_base = urlsplit(DEFAULT_OAUTH_API_BASE_URL)

    assert auth.scheme == "https"
    assert auth.netloc == "api-sg.aliexpress.com"
    assert auth.path == "/oauth/authorize"
    assert "oauth.aliexpress.com" not in auth.netloc

    assert token_base.scheme == "https"
    assert token_base.netloc == "api-sg.aliexpress.com"
    assert "oauth.aliexpress.com" not in token_base.netloc

    # Token exchange usa base + /rest/auth/token/create no mesmo host.
    token_url = DEFAULT_OAUTH_API_BASE_URL.rstrip("/") + DEFAULT_OAUTH_API_PATH
    token = urlsplit(token_url)
    assert token.netloc == "api-sg.aliexpress.com"
    assert token.path == "/rest/auth/token/create"


# --------------------------------------------------------------------------
# Resolução da URL de callback (sem dependência de ALIEXPRESS_CALLBACK_URL)
# --------------------------------------------------------------------------
def test_get_callback_url_explicit_override(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_CALLBACK_URL", "https://explicit.com/cb")
    monkeypatch.setenv("VERCEL_BRANCH_URL", "branch.vercel.app")
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "prod.vercel.app")
    assert get_aliexpress_callback_url() == "https://explicit.com/cb"


def test_get_callback_url_branch(monkeypatch):
    monkeypatch.delenv("ALIEXPRESS_CALLBACK_URL", raising=False)
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("VERCEL_BRANCH_URL", "branch-xyz.vercel.app")
    monkeypatch.delenv("VERCEL_PROJECT_PRODUCTION_URL", raising=False)
    assert get_aliexpress_callback_url() == (
        "https://branch-xyz.vercel.app/integrations/aliexpress/callback"
    )


def test_get_callback_url_production_env_uses_production(monkeypatch):
    monkeypatch.delenv("ALIEXPRESS_CALLBACK_URL", raising=False)
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("VERCEL_BRANCH_URL", "branch-xyz.vercel.app")
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "prod-xyz.vercel.app")
    # Em produção a branch URL NUNCA deve vencer.
    assert get_aliexpress_callback_url() == (
        "https://prod-xyz.vercel.app/integrations/aliexpress/callback"
    )


def test_get_callback_url_production_fallback(monkeypatch):
    monkeypatch.delenv("ALIEXPRESS_CALLBACK_URL", raising=False)
    monkeypatch.delenv("VERCEL_BRANCH_URL", raising=False)
    monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "prod-xyz.vercel.app")
    assert get_aliexpress_callback_url() == (
        "https://prod-xyz.vercel.app/integrations/aliexpress/callback"
    )


def test_get_callback_url_missing_raises(monkeypatch):
    monkeypatch.delenv("ALIEXPRESS_CALLBACK_URL", raising=False)
    monkeypatch.delenv("VERCEL_BRANCH_URL", raising=False)
    monkeypatch.delenv("VERCEL_PROJECT_PRODUCTION_URL", raising=False)
    with pytest.raises(AliExpressConfigError):
        get_aliexpress_callback_url()


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


def test_open_platform_timestamp_is_epoch_ms(monkeypatch):
    import time as time_module

    monkeypatch.setattr(time_module, "time", lambda: 1517820392.123)
    ts = _now_open_platform_timestamp()
    assert ts == "1517820392123"
    assert ts.isdigit() is True
    assert len(ts) == 13


def test_exchange_timestamp_enters_signature(monkeypatch):
    import time as time_module

    monkeypatch.setattr(time_module, "time", lambda: 1517820392.123)
    body = '{"access_token":"AT","expires_in":604800}'
    transport = _FakeOAuthTransport(body)
    exchange_authorization_code("k", "s", "CODE", transport=transport)
    # timestamp em epoch ms entra nos params antes da assinatura.
    assert transport.last_params["timestamp"] == "1517820392123"
    assert "sign" in transport.last_params
    assert transport.last_params["sign"]


ERROR_RESPONSE_FIXTURE = {
    "error_response": {
        "code": 123,
        "msg": "Invalid signature",
        "sub_code": "isp.invalid-signature",
        "sub_msg": "signature invalid",
        "request_id": "req-test",
    }
}


def test_normalize_oauth_token_error_response_shape():
    with pytest.raises(AliExpressAuthError) as excinfo:
        normalize_oauth_token(ERROR_RESPONSE_FIXTURE)
    exc = excinfo.value
    assert exc.code == 123
    assert exc.sub_code == "isp.invalid-signature"
    assert isinstance(exc.raw, dict)
    assert exc.raw.get("request_id") == "req-test"
    assert exc.raw.get("sub_code") == "isp.invalid-signature"
    # Mensagem sanitizada (sem code OAuth/state/token).
    assert "Invalid signature" in exc.message
    assert exc.raw.get("sub_msg") == "signature invalid"


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
    from urllib.parse import urlsplit, parse_qs

    parts = urlsplit(loc)
    assert parts.netloc == "api-sg.aliexpress.com"
    assert parts.path == "/oauth/authorize"
    q = parse_qs(parts.query)
    assert q.get("response_type") == ["code"]
    assert q.get("force_auth") == ["true"]
    assert q.get("redirect_uri") == [
        "https://www.pollomodas.com.br/integrations/aliexpress/callback"
    ]
    assert "sp" not in q
    assert "view" not in q


def test_authorize_canonicalizes_host_before_auth(auth_app, monkeypatch):
    # Não autenticado, acessando a deployment URL em Preview: a canonicalização
    # deve redirecionar para a branch URL ANTES de acionar admin_required.
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("VERCEL_BRANCH_URL", "pollo-git-feature.vercel.app")
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "k")
    monkeypatch.delenv("ALIEXPRESS_CALLBACK_URL", raising=False)
    client = auth_app.test_client()
    r = client.get(
        "/integrations/aliexpress/authorize",
        headers={"Host": "pollo-random-deploy.vercel.app"},
    )
    assert r.status_code == 302
    assert r.headers["Location"] == (
        "https://pollo-git-feature.vercel.app/integrations/aliexpress/authorize"
    )
    assert r.headers["Location"] != "/login"
    # Sem state, sem consulta de usuário, sem OAuth no host errado.
    with client.session_transaction(headers={"Host": "pollo-random-deploy.vercel.app"}) as sess:
        assert STATE_KEY not in sess


def test_authorize_production_does_not_canonicalize(auth_app, monkeypatch):
    # Produção: pollomodas.com.br NUNCA deve ser redirecionado para *-git-*.
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("VERCEL_BRANCH_URL", "pollo-git-feature.vercel.app")
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "k")
    monkeypatch.delenv("ALIEXPRESS_CALLBACK_URL", raising=False)
    client = auth_app.test_client()
    r = client.get(
        "/integrations/aliexpress/authorize",
        headers={"Host": "pollomodas.com.br"},
    )
    assert r.status_code == 302
    assert r.headers["Location"] != (
        "https://pollo-git-feature.vercel.app/integrations/aliexpress/authorize"
    )
    # Sem auth no host estável -> login (admin_required roda).
    assert r.headers["Location"].endswith("/login")


def test_authorize_branch_host_unauthenticated_goes_to_login(auth_app, monkeypatch):
    # No host estável (branch), sem auth -> login (admin_required roda).
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("VERCEL_BRANCH_URL", "pollo-git-feature.vercel.app")
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "k")
    monkeypatch.delenv("ALIEXPRESS_CALLBACK_URL", raising=False)
    client = auth_app.test_client()
    r = client.get(
        "/integrations/aliexpress/authorize",
        headers={"Host": "pollo-git-feature.vercel.app"},
    )
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/login")


def test_authorize_on_branch_host_generates_state_and_redirects(auth_app, monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("VERCEL_BRANCH_URL", "pollo-git-feature.vercel.app")
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "k")
    monkeypatch.delenv("ALIEXPRESS_CALLBACK_URL", raising=False)
    client = auth_app.test_client()
    client.get(
        "/_test_login?role=admin",
        headers={"Host": "pollo-git-feature.vercel.app"},
    )
    r = client.get(
        "/integrations/aliexpress/authorize",
        headers={"Host": "pollo-git-feature.vercel.app"},
    )
    assert r.status_code == 302
    loc = r.headers["Location"]
    from urllib.parse import parse_qs, urlsplit

    assert loc.startswith(DEFAULT_OAUTH_AUTHORIZE_URL)
    q = parse_qs(urlsplit(loc).query)
    assert q.get("redirect_uri") == [
        "https://pollo-git-feature.vercel.app/integrations/aliexpress/callback"
    ]
    # State foi efetivamente criado neste segundo passo.
    with client.session_transaction(headers={"Host": "pollo-git-feature.vercel.app"}) as sess:
        assert STATE_KEY in sess


def test_authorize_diagnostic_logs_no_secrets(auth_app, monkeypatch, caplog):
    import logging

    from urllib.parse import parse_qs, urlsplit

    branch = "pollo-modas-git-feature-aliexp-34cbfb-danilos-projects-dc14d397.vercel.app"
    app_key_value = "supersecretappkey123"
    app_secret_value = "supersecretappsecret456"
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("VERCEL_BRANCH_URL", branch)
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", app_key_value)
    monkeypatch.setenv("ALIEXPRESS_APP_SECRET", app_secret_value)
    monkeypatch.delenv("ALIEXPRESS_CALLBACK_URL", raising=False)

    client = auth_app.test_client()
    client.get("/_test_login?role=admin", headers={"Host": branch})
    with caplog.at_level(logging.INFO):
        r = client.get(
            "/integrations/aliexpress/authorize",
            headers={"Host": branch},
        )
    assert r.status_code == 302

    # Captura o state real da URL de redirecionamento para provar que não vaza.
    state_value = parse_qs(urlsplit(r.headers["Location"]).query)["state"][0]

    log_text = caplog.text
    # Campos esperados presentes (sanitizados).
    assert "authorize_host=api-sg.aliexpress.com" in log_text
    assert "authorize_path=/oauth/authorize" in log_text
    assert ("callback_host=%s" % branch) in log_text
    assert "callback_path=/integrations/aliexpress/callback" in log_text
    assert "params=['client_id', 'force_auth', 'redirect_uri', 'response_type', 'state']" in log_text

    # Nenhum dado sensível deve aparecer nos logs.
    assert app_key_value not in log_text
    assert app_secret_value not in log_text
    assert state_value not in log_text


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


def test_callback_oauth_failure_shows_sanitized(app_client, monkeypatch, caplog):
    import logging

    real_code = "REAL_OAUTH_CODE_secret123"
    real_state = "REAL_STATE_secret456"
    # Simula o comportamento Vercel: reduz o log de acesso do Werkzeug.
    monkeypatch.setattr(
        logging.getLogger("werkzeug"), "level", logging.WARNING
    )
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "k")
    monkeypatch.setenv("ALIEXPRESS_APP_SECRET", "s")
    monkeypatch.setenv("ALIEXPRESS_CALLBACK_URL", "https://x.com/cb")

    def _raise(*a, **k):
        return normalize_oauth_token(ERROR_RESPONSE_FIXTURE)

    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.exchange_authorization_code", _raise
    )
    with app_client.session_transaction() as sess:
        sess[STATE_KEY] = {"state": real_state, "ts": time.time()}
    with caplog.at_level(logging.INFO):
        r = app_client.get(
            f"/integrations/aliexpress/callback?code={real_code}&state={real_state}"
        )
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "code: 123" in body
    assert "sub_code: isp.invalid-signature" in body
    assert "request_id: req-test" in body
    # Nenhum dado sensível (code OAuth/state/token) deve aparecer nos logs.
    log_text = caplog.text
    assert real_code not in log_text
    assert real_state not in log_text


def test_create_app_lowers_werkzeug_level_on_vercel(monkeypatch):
    import logging

    lg = logging.getLogger("werkzeug")
    monkeypatch.setattr(lg, "level", logging.WARNING)
    monkeypatch.setenv("VERCEL", "1")
    from loja import create_app

    create_app()
    assert lg.level == logging.WARNING
