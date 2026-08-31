"""Testes offline do fluxo OAuth AliExpress (sem chamadas de rede reais)."""

from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from flask import Blueprint, Flask, request

from loja.integrations.aliexpress import (
    AliExpressFreightOption,
    AliExpressFreightResult,
    AliExpressProductDetail,
    AliExpressProductSku,
    AliExpressSearchResult,
)
from loja.integrations.aliexpress.errors import (
    AliExpressAPIError,
    AliExpressAuthError,
    AliExpressConfigError,
    AliExpressRateLimitError,
)
from loja.integrations.aliexpress.transport import TransportError
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
    RequestsOAuthTransport,
)
from loja.integrations.aliexpress.routes import (
    OAUTH_CALLBACK_COOKIE_PATH,
    OAUTH_CODE_COOKIE,
    OAUTH_STATE_COOKIE,
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


def _fake_product_detail():
    return AliExpressProductDetail(
        product_id="100500123",
        title="Test product",
        description="desc",
        category_id="123",
        images=["https://x/a.jpg"],
        video="https://x/v.mp4",
        skus=[
            AliExpressProductSku(
                sku_id="1",
                available_stock=5,
                sale_price=Decimal("9.90"),
                original_price=Decimal("19.90"),
                currency="BRL",
            )
        ],
        attributes=[],
        available_stock=5,
        sale_price=Decimal("9.90"),
        original_price=Decimal("19.90"),
        currency="BRL",
    )


def _fake_freight_result():
    return AliExpressFreightResult(
        options=[
            AliExpressFreightOption(
                service_name="Cainiao",
                company="AliExpress Standard Shipping",
                shipping_fee=Decimal("12.50"),
                currency="BRL",
                delivery_days=15,
                tracking=True,
            )
        ],
        currency="BRL",
        min_fee=Decimal("12.50"),
        min_delivery_days=15,
    )


class _SearchProductClient:
    """Cliente falso que responde busca, detalhe e frete (smoke Fase 4)."""

    def __init__(self, *a, **k):
        pass

    def search_products(self, **kw):
        return _fake_result(3)

    def get_product(self, **kw):
        return _fake_product_detail()

    def query_freight(self, **kw):
        return _fake_freight_result()


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


def test_authorize_logs_no_secrets(auth_app, monkeypatch, caplog):
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
    # Nenhum dado sensível (app_key, app_secret, state) deve aparecer nos logs.
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
        "loja.integrations.aliexpress.routes.AliExpressClient", _SearchProductClient
    )
    # Opt-in explícito: este teste valida o histórico do smoke DS de frete.
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes._freight_smoke_enabled", lambda: True
    )
    state = "ST1"
    with app_client.session_transaction() as sess:
        sess[STATE_KEY] = {"state": state, "ts": time.time()}
    r = app_client.get(f"/integrations/aliexpress/callback?code=ABC123&state={state}")
    body = r.data.decode()
    assert r.status_code == 200
    assert "DS search: success" in body
    assert "produtos recebidos: 3" in body
    assert "DS product.get: success" in body
    assert "SKUs recebidos: 1" in body
    assert "imagens recebidos: 1" in body
    assert "DS freight.query: success" in body
    assert "candidatos testados: 1" in body
    assert "opções de frete: 1" in body
    assert "frete mínimo: presente" in body
    assert "moeda: presente" in body
    assert "prazo: presente" in body
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
        "loja.integrations.aliexpress.routes.AliExpressClient", _SearchProductClient
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


def test_callback_freight_failure_shows_sanitized(app_client, monkeypatch):
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.exchange_authorization_code", lambda *a, **k: _fake_token()
    )

    class _FailFreightClient:
        def __init__(self, *a, **k):
            pass

        def search_products(self, **kw):
            return _fake_result(3)

        def get_product(self, **kw):
            return _fake_product_detail()

        def query_freight(self, **kw):
            raise AliExpressAPIError(
                "SelectSkuId is Required", code=507, raw={"request_id": "RIDF"}
            )

    monkeypatch.setattr("loja.integrations.aliexpress.routes.AliExpressClient", _FailFreightClient)
    # Opt-in explícito: teste do caminho de falha do smoke (histórico).
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes._freight_smoke_enabled", lambda: True
    )
    state = "ST_FREIGHT"
    with app_client.session_transaction() as sess:
        sess[STATE_KEY] = {"state": state, "ts": time.time()}
    r = app_client.get(f"/integrations/aliexpress/callback?code=ABC&state={state}")
    body = r.data.decode()
    assert r.status_code == 200
    assert "DS product.get: success" in body
    assert "DS freight.query: failure" in body
    assert "code: 507" in body
    assert "SelectSkuId is Required" in body  # mensagem sanitizada é exibida
    assert "RIDF" in body  # request_id pode ser exibido (não é secreto)
    assert "REAL_AT" not in body  # token nunca exibido


# --------------------------------------------------------------------------
# Smoke de frete (Fase 4): sondagem de candidatos (produto x SKU)
# --------------------------------------------------------------------------
def _probe_search_result(product_ids):
    return AliExpressSearchResult(
        products=[SimpleNamespace(item_id=pid) for pid in product_ids],
        total=len(product_ids),
        wrapper="aliexpress.ds.text.search_response",
    )


def _probe_product_detail(pid, sku_ids):
    return AliExpressProductDetail(
        product_id=pid,
        title="p",
        images=["https://x/a.jpg"],
        skus=[AliExpressProductSku(sku_id=s, available_stock=5) for s in sku_ids],
    )


def _probe_product_detail_skus(pid, skus):
    # skus: list of (sku_id, available_stock)
    return AliExpressProductDetail(
        product_id=pid,
        title="p",
        images=["https://x/a.jpg"],
        skus=[AliExpressProductSku(sku_id=s, available_stock=stock) for s, stock in skus],
    )


def _probe_product_raw_skus(pid, skus, raw=None):
    # skus: list of (sku_id, available_stock), sku_id já no formato canônico
    # AliExpress (<n>:<n>[;<n>:<n>]) como retornado pelo ds.product.get.
    return AliExpressProductDetail(
        product_id=pid,
        title="p",
        images=["https://x/a.jpg"],
        skus=[AliExpressProductSku(sku_id=s, available_stock=stock) for s, stock in skus],
        raw=raw or {},
    )


def _probe_freight_success():
    return AliExpressFreightResult(
        options=[
            AliExpressFreightOption(
                shipping_fee=Decimal("5.00"), currency="BRL", delivery_days=10
            )
        ],
        currency="BRL",
        min_fee=Decimal("5.00"),
        min_delivery_days=10,
    )


def _probe_buyer_success():
    return AliExpressFreightResult(
        options=[
            AliExpressFreightOption(
                service_name="Cainiao",
                shipping_fee=Decimal("7.50"),
                currency="BRL",
                delivery_days=12,
            )
        ],
        currency="BRL",
        min_fee=Decimal("7.50"),
        min_delivery_days=12,
    )


class _ProbeClient:
    """Cliente falso configurável para exercitar a sondagem de frete."""

    def __init__(self, search_result, get_map, freight_map, call_log, buyer_map=None):
        self._search = search_result
        self._get = get_map
        self._freight = freight_map
        self.calls = call_log
        self.get_calls = []
        self.freight_kwargs = []
        self._buyer = buyer_map or {}
        self.buyer_calls = []
        self.buyer_get_calls = []

    def search_products(self, **kw):
        return self._search

    def get_product(self, *, product_id, **kw):
        self.get_calls.append(product_id)
        return self._get[product_id]

    def query_freight(self, *, product_id, selected_sku_id, **kw):
        self.calls.append((product_id, selected_sku_id))
        self.freight_kwargs.append(kw)
        outcome = self._freight.get((product_id, selected_sku_id))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def calculate_buyer_freight(self, *, product_id, **kw):
        self.buyer_calls.append((product_id, kw))
        outcome = self._buyer.get(product_id)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get_buyer_freight(self, *, product_id, sku_id=None, **kw):
        self.buyer_get_calls.append((product_id, sku_id, kw))
        outcome = self._buyer.get(product_id)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _run_probe(app_client, monkeypatch, probe_client):
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.exchange_authorization_code",
        lambda *a, **k: _fake_token(),
    )
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.AliExpressClient",
        lambda *a, **k: probe_client,
    )
    # O smoke de frete está ENCERRADO em runtime (_freight_smoke_enabled() ->
    # False). Estes testes de sondagem fazem opt-in explícito para manter a
    # cobertura diagnóstica do caminho de frete no callback.
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes._freight_smoke_enabled",
        lambda: True,
    )
    state = "ST_PROBE"
    with app_client.session_transaction() as sess:
        sess[STATE_KEY] = {"state": state, "ts": time.time()}
    r = app_client.get(f"/integrations/aliexpress/callback?code=ABC&state={state}")
    return r.data.decode()


def test_freight_probe_first_sku_501_then_success(app_client, monkeypatch):
    calls = []
    get_map = {"p1": _probe_product_detail("p1", ["s1", "s2"])}
    freight = {
        ("p1", "s1"): AliExpressAPIError("DELIVERY_INFO_EMPTY", code=501),
        ("p1", "s2"): _probe_freight_success(),
    }
    client = _ProbeClient(_probe_search_result(["p1"]), get_map, freight, calls)
    body = _run_probe(app_client, monkeypatch, client)
    assert "DS freight.query: success" in body
    assert "candidatos testados: 2" in body
    assert "opções de frete: 1" in body
    assert calls == [("p1", "s1"), ("p1", "s2")]


def test_freight_probe_505_then_success(app_client, monkeypatch):
    calls = []
    get_map = {"p1": _probe_product_detail("p1", ["s1", "s2"])}
    freight = {
        ("p1", "s1"): AliExpressAPIError(
            "DELIVERY_NOT_AVAILABLE_TO_YOUR_ADDRESS", code=505
        ),
        ("p1", "s2"): _probe_freight_success(),
    }
    client = _ProbeClient(_probe_search_result(["p1"]), get_map, freight, calls)
    body = _run_probe(app_client, monkeypatch, client)
    assert "DS freight.query: success" in body
    assert "candidatos testados: 2" in body


def test_freight_probe_507_aborts(app_client, monkeypatch):
    calls = []
    get_map = {"p1": _probe_product_detail("p1", ["s1", "s2"])}
    freight = {
        ("p1", "s1"): AliExpressAPIError("SelectSkuId is Required", code=507),
    }
    client = _ProbeClient(_probe_search_result(["p1"]), get_map, freight, calls)
    body = _run_probe(app_client, monkeypatch, client)
    assert "DS freight.query: failure" in body
    assert "code: 507" in body
    assert "SelectSkuId is Required" in body
    assert calls == [("p1", "s1")]  # não tentou o próximo SKU


def test_freight_probe_apicalllimit_aborts(app_client, monkeypatch):
    calls = []
    get_map = {"p1": _probe_product_detail("p1", ["s1", "s2"])}
    freight = {
        ("p1", "s1"): AliExpressRateLimitError("too many requests", code=555),
    }
    client = _ProbeClient(_probe_search_result(["p1"]), get_map, freight, calls)
    body = _run_probe(app_client, monkeypatch, client)
    assert "DS freight.query: failure" in body
    assert calls == [("p1", "s1")]


def test_freight_probe_limits_to_two_skus(app_client, monkeypatch):
    calls = []
    # 3 SKUs no detalhe, mas o smoke só sonda os 2 primeiros.
    get_map = {"p1": _probe_product_detail("p1", ["s1", "s2", "s3"])}
    freight = {
        ("p1", "s1"): AliExpressAPIError("DELIVERY_INFO_EMPTY", code=501),
        ("p1", "s2"): AliExpressAPIError("DELIVERY_INFO_EMPTY", code=501),
        ("p1", "s3"): _probe_freight_success(),  # nunca deve ser chamado
    }
    client = _ProbeClient(_probe_search_result(["p1"]), get_map, freight, calls)
    body = _run_probe(app_client, monkeypatch, client)
    assert "DS freight.query: unavailable" in body
    assert "code 501: 2" in body
    assert len(calls) == 2
    assert ("p1", "s3") not in calls


def test_freight_probe_success_stops_immediately(app_client, monkeypatch):
    calls = []
    get_map = {"p1": _probe_product_detail("p1", ["s1", "s2"])}
    freight = {("p1", "s1"): _probe_freight_success()}
    client = _ProbeClient(_probe_search_result(["p1"]), get_map, freight, calls)
    body = _run_probe(app_client, monkeypatch, client)
    assert "DS freight.query: success" in body
    assert "candidatos testados: 1" in body
    assert calls == [("p1", "s1")]


def test_freight_probe_no_secrets_in_logs(app_client, monkeypatch, caplog):
    import logging

    calls = []
    get_map = {"p1": _probe_product_detail("p1", ["s1", "s2"])}
    freight = {
        ("p1", "s1"): AliExpressAPIError("DELIVERY_INFO_EMPTY", code=501),
        ("p1", "s2"): _probe_freight_success(),
    }
    client = _ProbeClient(_probe_search_result(["p1"]), get_map, freight, calls)
    with caplog.at_level(logging.INFO):
        body = _run_probe(app_client, monkeypatch, client)
    log_text = caplog.text
    # Nenhum ID de produto/SKU, token, code OAuth ou state nos logs.
    assert "p1" not in log_text
    assert "s1" not in log_text
    assert "REAL_AT" not in body


def test_freight_probe_reuses_single_get_product(app_client, monkeypatch):
    calls = []
    get_map = {"p1": _probe_product_detail("p1", ["s1", "s2"])}
    freight = {
        ("p1", "s1"): AliExpressAPIError("DELIVERY_INFO_EMPTY", code=501),
        ("p1", "s2"): _probe_freight_success(),
    }
    client = _ProbeClient(_probe_search_result(["p1"]), get_map, freight, calls)
    _run_probe(app_client, monkeypatch, client)
    # Phase 3 faz 1 get_product; o smoke de frete NÃO chama get_product de novo
    # e NÃO percorre outros produtos.
    assert client.get_calls == ["p1"]


def test_freight_probe_no_sleep_in_callback(app_client, monkeypatch):
    import time as time_module

    sleeps = []
    monkeypatch.setattr(time_module, "sleep", lambda s: sleeps.append(s))
    calls = []
    get_map = {"p1": _probe_product_detail("p1", ["s1", "s2"])}
    freight = {
        ("p1", "s1"): AliExpressAPIError("DELIVERY_INFO_EMPTY", code=501),
        ("p1", "s2"): _probe_freight_success(),
    }
    client = _ProbeClient(_probe_search_result(["p1"]), get_map, freight, calls)
    _run_probe(app_client, monkeypatch, client)
    # Nenhum sleep no callback (mantém dentro do timeout).
    assert sleeps == []


def test_freight_probe_passes_destination_from_env(app_client, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("ALIEXPRESS_SMOKE_PROVINCE_CODE", "SP")
    monkeypatch.setenv("ALIEXPRESS_SMOKE_CITY_CODE", "SAO_PAULO")
    calls = []
    get_map = {"p1": _probe_product_detail("p1", ["s1", "s2"])}
    freight = {("p1", "s1"): _probe_freight_success()}
    client = _ProbeClient(_probe_search_result(["p1"]), get_map, freight, calls)
    with caplog.at_level(logging.INFO):
        body = _run_probe(app_client, monkeypatch, client)
    assert "DS freight.query: success" in body
    # Valores de destino chegaram ao query_freight.
    assert client.freight_kwargs[0]["province_code"] == "SP"
    assert client.freight_kwargs[0]["city_code"] == "SAO_PAULO"
    # Valores NÃO vazam em body nem em logs.
    assert "SP" not in body
    assert "SAO_PAULO" not in body
    assert "SP" not in caplog.text
    assert "SAO_PAULO" not in caplog.text


def test_freight_probe_summary_shows_provided_flags(app_client, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("ALIEXPRESS_SMOKE_PROVINCE_CODE", "SP")
    monkeypatch.setenv("ALIEXPRESS_SMOKE_CITY_CODE", "SAO_PAULO")
    calls = []
    get_map = {"p1": _probe_product_detail("p1", ["s1", "s2"])}
    freight = {
        ("p1", "s1"): AliExpressAPIError("DELIVERY_INFO_EMPTY", code=501),
        ("p1", "s2"): AliExpressAPIError("DELIVERY_INFO_EMPTY", code=501),
    }
    client = _ProbeClient(_probe_search_result(["p1"]), get_map, freight, calls)
    with caplog.at_level(logging.INFO):
        body = _run_probe(app_client, monkeypatch, client)
    assert "DS freight.query: unavailable" in body
    assert "province_provided=yes" in caplog.text
    assert "city_provided=yes" in caplog.text
    # Valores ainda não vazam.
    assert "SP" not in caplog.text
    assert "SAO_PAULO" not in caplog.text


def test_callback_buyer_calculate_success_camel_sku(app_client, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("ALIEXPRESS_SMOKE_BUYER_FREIGHT", "1")
    monkeypatch.setenv("ALIEXPRESS_SMOKE_SEND_GOODS_COUNTRY_CODE", "CN")
    calls = []
    get_map = {"100500123": _probe_product_raw_skus("100500123", [("14:771", 5)])}
    buyer = {"100500123": _probe_buyer_success()}
    client = _ProbeClient(
        _probe_search_result(["100500123"]), get_map, {}, calls, buyer_map=buyer
    )
    with caplog.at_level(logging.INFO):
        body = _run_probe(app_client, monkeypatch, client)
    assert "Buyer freight.calculate: success" in body
    assert "candidatos testados: 1" in body
    assert "opções de frete: 1" in body
    assert "frete mínimo: presente" in body
    assert "moeda: presente" in body
    assert "prazo: presente" in body
    # Uma única freight.calculate (/sync, canônico); freight.get e ds.freight.query NÃO chamados.
    assert len(client.buyer_calls) == 1
    assert client.buyer_get_calls == []
    assert client.calls == []
    # search/product.get continuam pelo cliente /sync (probe client).
    assert client.get_calls == ["100500123"]
    # sku.id bruto canônico + sku_key camelCase no request; sem province/city.
    pid, kw = client.buyer_calls[0]
    assert pid == "100500123"
    assert kw["sku_id"] == "14:771"
    assert kw["sku_key"] == "skuId"
    assert kw["country_code"] == "BR"
    assert kw["product_num"] == 1
    assert kw["send_goods_country_code"] == "CN"
    assert "province_code" not in kw
    assert "city_code" not in kw
    # Diagnóstico seguro da forma: flags somente, sem IDs/SKU/payload.
    assert "buyer freight.calculate request shape:" in caplog.text
    assert "sku_present=yes" in caplog.text
    assert "sku_key=camel" in caplog.text
    assert "raw_sku_canonical=yes" in caplog.text
    assert "product_id_numeric=yes" in caplog.text
    assert "province_sent=no" in caplog.text
    assert "city_sent=no" in caplog.text
    assert "14:771" not in body
    assert "14:771" not in caplog.text
    # Nenhum segredo/ID em logs.
    assert "REAL_AT" not in caplog.text


def test_callback_buyer_calculate_omits_province_city_even_when_env_set(
    app_client, monkeypatch, caplog
):
    import logging

    monkeypatch.setenv("ALIEXPRESS_SMOKE_BUYER_FREIGHT", "1")
    monkeypatch.setenv("ALIEXPRESS_SMOKE_SEND_GOODS_COUNTRY_CODE", "CN")
    monkeypatch.setenv("ALIEXPRESS_SMOKE_PROVINCE_CODE", "SP")
    monkeypatch.setenv("ALIEXPRESS_SMOKE_CITY_CODE", "SAO_PAULO")
    calls = []
    get_map = {"100500123": _probe_product_raw_skus("100500123", [("14:771", 5)])}
    buyer = {"100500123": _probe_buyer_success()}
    client = _ProbeClient(
        _probe_search_result(["100500123"]), get_map, {}, calls, buyer_map=buyer
    )
    with caplog.at_level(logging.INFO):
        body = _run_probe(app_client, monkeypatch, client)
    assert "Buyer freight.calculate: success" in body
    # Mesmo com as envs definidas, province/city NÃO são enviados no smoke.
    _, kw = client.buyer_calls[0]
    assert "province_code" not in kw
    assert "city_code" not in kw
    assert "province_sent=no" in caplog.text
    assert "city_sent=no" in caplog.text
    # Valores NÃO vazam em body nem em logs.
    assert "SP" not in body
    assert "SAO_PAULO" not in body
    assert "SP" not in caplog.text
    assert "SAO_PAULO" not in caplog.text


def test_callback_buyer_calculate_skips_to_raw_canonical_sku(app_client, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("ALIEXPRESS_SMOKE_BUYER_FREIGHT", "1")
    monkeypatch.setenv("ALIEXPRESS_SMOKE_SEND_GOODS_COUNTRY_CODE", "CN")
    calls = []
    # Primeiro sem estoque (ignorado); segundo elegível com sku.id canônico.
    get_map = {"100500123": _probe_product_raw_skus("100500123", [("1:1", 0), ("14:771", 5)])}
    buyer = {"100500123": _probe_buyer_success()}
    client = _ProbeClient(
        _probe_search_result(["100500123"]), get_map, {}, calls, buyer_map=buyer
    )
    with caplog.at_level(logging.INFO):
        body = _run_probe(app_client, monkeypatch, client)
    assert "Buyer freight.calculate: success" in body
    # Somente UMA freight.calculate no callback, com o sku.id bruto do elegível.
    assert len(client.buyer_calls) == 1
    _, kw = client.buyer_calls[0]
    assert kw["sku_id"] == "14:771"
    assert kw["sku_key"] == "skuId"
    # IDs/valores não vazam em body nem em logs.
    assert "1:1" not in body
    assert "1:1" not in caplog.text
    assert "14:771" not in body
    assert "14:771" not in caplog.text


def test_callback_buyer_calculate_unavailable_without_canonical_sku(
    app_client, monkeypatch, caplog
):
    import logging

    monkeypatch.setenv("ALIEXPRESS_SMOKE_BUYER_FREIGHT", "1")
    monkeypatch.setenv("ALIEXPRESS_SMOKE_SEND_GOODS_COUNTRY_CODE", "CN")
    calls = []
    # sku.id local (não canônico) -> nenhum SKU canônico válido.
    get_map = {"100500123": _probe_product_detail("100500123", ["s1"])}
    client = _ProbeClient(
        _probe_search_result(["100500123"]), get_map, {}, calls, buyer_map={}
    )
    with caplog.at_level(logging.INFO):
        body = _run_probe(app_client, monkeypatch, client)
    assert "Buyer freight.calculate: unavailable" in body
    assert "motivo: nenhum SKU elegível com SKU canônico válido" in body
    # Quando não há SKU canônico, NÃO chama a API.
    assert client.buyer_calls == []
    assert client.buyer_get_calls == []
    # IDs/valores não vazam em body nem em logs.
    assert "s0" not in body
    assert "s1" not in body
    assert "s0" not in caplog.text
    assert "s1" not in caplog.text


def test_callback_buyer_calculate_provider_unavailable(app_client, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("ALIEXPRESS_SMOKE_BUYER_FREIGHT", "1")
    monkeypatch.setenv("ALIEXPRESS_SMOKE_SEND_GOODS_COUNTRY_CODE", "CN")
    calls = []
    get_map = {"100500123": _probe_product_raw_skus("100500123", [("14:771", 5)])}
    # queryShippingOptionForItem(forBuyer...) retornou null => resultado vazio.
    buyer = {"100500123": AliExpressFreightResult(options=[])}
    client = _ProbeClient(
        _probe_search_result(["100500123"]), get_map, {}, calls, buyer_map=buyer
    )
    with caplog.at_level(logging.INFO):
        body = _run_probe(app_client, monkeypatch, client)
    assert "Buyer freight.calculate: unavailable" in body
    assert "provider_unavailable" in body
    assert "shipping_cost = None" in body
    assert "nunca frete zero" in body
    # Uma única chamada tentada; NUNCA se converte em frete/sucesso vazio.
    assert len(client.buyer_calls) == 1
    assert client.buyer_get_calls == []
    assert client.calls == []
    assert "14:771" not in caplog.text


def test_callback_buyer_calculate_failure_sanitized(app_client, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("ALIEXPRESS_SMOKE_BUYER_FREIGHT", "1")
    monkeypatch.setenv("ALIEXPRESS_SMOKE_SEND_GOODS_COUNTRY_CODE", "CN")
    calls = []
    get_map = {"100500123": _probe_product_raw_skus("100500123", [("14:771", 5)])}
    buyer = {"100500123": AliExpressAPIError("method not found", code=27, sub_code="isp.x")}
    client = _ProbeClient(
        _probe_search_result(["100500123"]), get_map, {}, calls, buyer_map=buyer
    )
    with caplog.at_level(logging.INFO):
        body = _run_probe(app_client, monkeypatch, client)
    assert "Buyer freight.calculate: failure" in body
    assert "code: 27" in body
    assert "sub_code: isp.x" in body
    assert client.buyer_get_calls == []
    assert "14:771" not in caplog.text
    assert "REAL_AT" not in caplog.text


def test_callback_freight_smoke_disabled_on_preview(app_client, monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("ALIEXPRESS_SMOKE_BUYER_FREIGHT", "1")
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.exchange_authorization_code",
        lambda *a, **k: _fake_token(),
    )
    calls = []
    get_map = {"100500123": _probe_product_raw_skus("100500123", [("14:771", 5)])}
    client = _ProbeClient(
        _probe_search_result(["100500123"]), get_map, {}, calls, buyer_map={}
    )
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.AliExpressClient",
        lambda *a, **k: client,
    )
    state = "ST_PREV"
    with app_client.session_transaction() as sess:
        sess[STATE_KEY] = {"state": state, "ts": time.time()}
    body = app_client.get(
        f"/integrations/aliexpress/callback?code=ABC&state={state}"
    ).data.decode()
    assert "Smoke de frete: desativado (investigação encerrada)" in body
    # NENHUMA chamada de frete no Preview (nem buyer, nem ds, nem freight.get).
    assert client.buyer_calls == []
    assert client.buyer_get_calls == []
    assert client.calls == []
    assert "14:771" not in body


def test_callback_freight_smoke_disabled_by_default_in_production(
    app_client, monkeypatch
):
    # Regressão Fase 5-fechamento: o smoke de frete está ENCERRADO em runtime.
    # Mesmo em "produção" (sem VERCEL_ENV, sem opt-in), NENHUMA chamada de
    # frete acontece e a resposta avisa que a investigação foi encerrada.
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.exchange_authorization_code",
        lambda *a, **k: _fake_token(),
    )
    calls = []
    get_map = {"100500123": _probe_product_raw_skus("100500123", [("14:771", 5)])}
    buyer = {"100500123": _probe_buyer_success()}
    client = _ProbeClient(
        _probe_search_result(["100500123"]), get_map, {}, calls, buyer_map=buyer
    )
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.AliExpressClient",
        lambda *a, **k: client,
    )
    state = "ST_PROD"
    with app_client.session_transaction() as sess:
        sess[STATE_KEY] = {"state": state, "ts": time.time()}
    body = app_client.get(
        f"/integrations/aliexpress/callback?code=ABC&state={state}"
    ).data.decode()
    assert "Smoke de frete: desativado (investigação encerrada)" in body
    assert client.buyer_calls == []
    assert client.buyer_get_calls == []
    assert client.calls == []
    assert "14:771" not in body
    # O gate em runtime é absoluto: nunca roda automaticamente.
    from loja.integrations.aliexpress.routes import _freight_smoke_enabled

    assert _freight_smoke_enabled() is False


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


def test_create_app_disables_werkzeug_access_log_on_vercel(monkeypatch):
    import logging

    lg = logging.getLogger("werkzeug")
    monkeypatch.setenv("VERCEL", "1")
    from loja import create_app

    create_app()
    assert lg.disabled is True


def test_create_app_keeps_werkzeug_access_log_outside_vercel(monkeypatch):
    import logging

    lg = logging.getLogger("werkzeug")
    monkeypatch.delenv("VERCEL", raising=False)
    # Aplica-se ANTES de servir: sem VERCEL, o access log continua ligado.
    lg.disabled = False
    try:
        monkeypatch.setenv("VERCEL_ENV", "preview")
        from loja import create_app
        create_app()
        assert lg.disabled is False
    finally:
        lg.disabled = False


class _AccessLogStub:
    from werkzeug.serving import WSGIRequestHandler

    command = "GET"
    request_version = "HTTP/1.1"
    _control_char_table = WSGIRequestHandler._control_char_table

    def __init__(self, path, requestline):
        self.path = path
        self.requestline = requestline
        self.lines = []

    def address_string(self):
        return "127.0.0.1"

    def log_date_time_string(self):
        return "01/Jan/2026 00:00:00"

    def log(self, log_type, message, *args):
        self.lines.append((message % args, self.path, self.requestline))


def _emit_access_log(path, requestline):
    from werkzeug.serving import WSGIRequestHandler

    stub = _AccessLogStub(path, requestline)
    WSGIRequestHandler.log_request(stub)
    return stub


def _ensure_werkzeug_hardened(monkeypatch):
    from loja import _harden_werkzeug_access_log

    monkeypatch.setenv("VERCEL", "1")
    _harden_werkzeug_access_log()


def test_access_log_sanitizes_oauth_callback_query(monkeypatch):
    _ensure_werkzeug_hardened(monkeypatch)
    original = "/integrations/aliexpress/callback?code=AAA&state=BBB"
    stub = _emit_access_log(original, "GET /integrations/aliexpress/callback?code=AAA&state=BBB HTTP/1.1")
    assert len(stub.lines) == 1
    line, path_at_emit, requestline_at_emit = stub.lines[0]
    assert "?code=" not in line
    assert "&state=" not in line
    assert "/integrations/aliexpress/callback HTTP/1.1" in line
    # Durante a emissão o path/requestline estão redigidos...
    assert path_at_emit == "/integrations/aliexpress/callback"
    assert requestline_at_emit == "GET /integrations/aliexpress/callback HTTP/1.1"
    # ...e restaurados depois de log_request (a request da app não muda).
    assert stub.path == original
    assert stub.requestline == "GET /integrations/aliexpress/callback?code=AAA&state=BBB HTTP/1.1"


def test_access_log_other_route_keeps_query(monkeypatch):
    _ensure_werkzeug_hardened(monkeypatch)
    stub = _emit_access_log("/products?page=2", "GET /products?page=2 HTTP/1.1")
    line, path_at_emit, _ = stub.lines[0]
    assert "?page=2" in line
    assert path_at_emit == "/products?page=2"
    assert stub.path == "/products?page=2"


def test_access_log_callback_without_query_intact(monkeypatch):
    _ensure_werkzeug_hardened(monkeypatch)
    stub = _emit_access_log(
        "/integrations/aliexpress/callback", "GET /integrations/aliexpress/callback HTTP/1.1"
    )
    line, path_at_emit, requestline_at_emit = stub.lines[0]
    assert "HTTPS/1.1" not in line
    assert "/integrations/aliexpress/callback HTTP/1.1" in line
    assert path_at_emit == "/integrations/aliexpress/callback"
    assert requestline_at_emit == "GET /integrations/aliexpress/callback HTTP/1.1"


def test_access_log_patch_applied_only_when_vercel(monkeypatch):
    from werkzeug.serving import WSGIRequestHandler

    monkeypatch.delenv("VERCEL", raising=False)
    WSGIRequestHandler._pollomodas_oauth_hardened = False
    from loja import _harden_werkzeug_access_log

    original = WSGIRequestHandler.log_request
    _harden_werkzeug_access_log()
    assert WSGIRequestHandler.log_request is original
    assert not getattr(WSGIRequestHandler, "_pollomodas_oauth_hardened", False)


def test_access_log_patch_idempotent(monkeypatch):
    from werkzeug.serving import WSGIRequestHandler

    _ensure_werkzeug_hardened(monkeypatch)
    first = WSGIRequestHandler.log_request
    from loja import _harden_werkzeug_access_log

    _harden_werkzeug_access_log()
    assert WSGIRequestHandler.log_request is first
    assert getattr(WSGIRequestHandler, "_pollomodas_oauth_hardened", False) is True


def test_vercel_callback_never_logs_oauth_query_string(monkeypatch):
    import logging

    monkeypatch.setenv("VERCEL", "1")
    lg = logging.getLogger("werkzeug")
    records = []

    class _Sink(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    sink = _Sink()
    lg.addHandler(sink)
    try:
        from loja import create_app

        test_app = create_app()
        # O access log do Werkzeug está desligado.
        assert lg.disabled is True
        client = test_app.test_client()
        # Callback continua respondendo normalmente (estado inválido => 400),
        # mas nenhuma linha do access log contém a query string OAuth.
        r = client.get("/integrations/aliexpress/callback?code=ABC&state=ST")
        assert r.status_code == 400
        lg.info('/callback?code=ABC&state=ST "GET / HTTP/1.1" 200')  # tentativa
        assert records == []
    finally:
        lg.disabled = False
        lg.removeHandler(sink)


def test_vercel_callback_still_receives_code_and_state(app_client, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("ALIEXPRESS_SMOKE_BUYER_FREIGHT", "1")
    monkeypatch.setenv("ALIEXPRESS_SMOKE_SEND_GOODS_COUNTRY_CODE", "CN")
    calls = []
    get_map = {"100500123": _probe_product_raw_skus("100500123", [("14:771", 5)])}
    buyer = {"100500123": _probe_buyer_success()}
    client = _ProbeClient(
        _probe_search_result(["100500123"]), get_map, {}, calls, buyer_map=buyer
    )
    with caplog.at_level(logging.INFO):
        body = _run_probe(app_client, monkeypatch, client)
    # O fluxo OAuth (code/state) chega intacto à aplicação mesmo com o patch.
    assert "Buyer freight.calculate: success" in body
    assert len(client.buyer_calls) == 1
    assert "14:771" not in caplog.text


# --------------------------------------------------------------------------
# Fallback de callback OAuth via cookie (middleware da Vercel)
# --------------------------------------------------------------------------
def _cookie_names(client):
    cookies = getattr(client, "_cookies", None)
    if cookies is None:
        return []
    return [storage_key[2] for storage_key in cookies]


def _run_probe_via_cookies(
    app_client,
    monkeypatch,
    probe_client,
    code,
    state,
    path="/integrations/aliexpress/callback",
    encoded=False,
):
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.exchange_authorization_code",
        lambda *a, **k: _fake_token(),
    )
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.AliExpressClient",
        lambda *a, **k: probe_client,
    )
    with app_client.session_transaction() as sess:
        sess[STATE_KEY] = {"state": state, "ts": time.time()}
    cookie_code = quote(code, safe="") if encoded else code
    app_client.set_cookie(OAUTH_CODE_COOKIE, cookie_code, path=OAUTH_CALLBACK_COOKIE_PATH)
    app_client.set_cookie(OAUTH_STATE_COOKIE, state, path=OAUTH_CALLBACK_COOKIE_PATH)
    return app_client.get(path)


def test_callback_recovers_code_and_state_from_cookies(app_client, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("ALIEXPRESS_SMOKE_BUYER_FREIGHT", "1")
    monkeypatch.setenv("ALIEXPRESS_SMOKE_SEND_GOODS_COUNTRY_CODE", "CN")
    # Opt-in explícito: teste do caminho Buyer voyage pelo callback via cookie.
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes._freight_smoke_enabled", lambda: True
    )
    calls = []
    get_map = {"100500123": _probe_product_raw_skus("100500123", [("14:771", 5)])}
    buyer = {"100500123": _probe_buyer_success()}
    client = _ProbeClient(
        _probe_search_result(["100500123"]), get_map, {}, calls, buyer_map=buyer
    )
    code = "PH_CODE"
    state = "PH_STATE"
    with caplog.at_level(logging.INFO):
        r = _run_probe_via_cookies(app_client, monkeypatch, client, code, state)
    body = r.data.decode()
    assert r.status_code == 200
    assert "Buyer freight.calculate: success" in body
    # Cookies apagados após processar o callback.
    assert OAUTH_CODE_COOKIE not in _cookie_names(app_client)
    assert OAUTH_STATE_COOKIE not in _cookie_names(app_client)
    # Nenhuma saída de log contém a query, o code ou o state (placeholders).
    assert "?code=" not in caplog.text
    assert "&state=" not in caplog.text
    assert code not in caplog.text
    assert state not in caplog.text
    assert "14:771" not in caplog.text


def test_callback_clean_reaches_flask_without_query_or_cookies(app_client):
    r = app_client.get("/integrations/aliexpress/callback")
    assert r.status_code == 400
    assert "Falha de validação de estado OAuth" in r.data.decode()


def test_callback_cookie_state_is_single_use(app_client, monkeypatch):
    calls = []
    get_map = {"100500123": _probe_product_detail("100500123", [])}
    client = _ProbeClient(_probe_search_result(["100500123"]), get_map, {}, calls)
    code = "PH_CODE"
    state = "PH_STATE"
    r1 = _run_probe_via_cookies(app_client, monkeypatch, client, code, state)
    assert r1.status_code == 200
    # O mesmo state re-apresentado pelos cookies é rejeitado (já consumido).
    app_client.set_cookie(OAUTH_CODE_COOKIE, code, path=OAUTH_CALLBACK_COOKIE_PATH)
    app_client.set_cookie(OAUTH_STATE_COOKIE, state, path=OAUTH_CALLBACK_COOKIE_PATH)
    r2 = app_client.get("/integrations/aliexpress/callback")
    assert r2.status_code == 400
    assert "Falha de validação de estado OAuth" in r2.data.decode()


def test_callback_cookies_cleared_after_processing(app_client, monkeypatch):
    calls = []
    get_map = {"100500123": _probe_product_detail("100500123", [])}
    client = _ProbeClient(_probe_search_result(["100500123"]), get_map, {}, calls)
    r = _run_probe_via_cookies(app_client, monkeypatch, client, "PH_CODE", "PH_STATE")
    assert r.status_code == 200
    cookies_after = _cookie_names(app_client)
    assert OAUTH_CODE_COOKIE not in cookies_after
    assert OAUTH_STATE_COOKIE not in cookies_after


def test_callback_cookie_path_preview_skips_freight(app_client, monkeypatch):
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("ALIEXPRESS_SMOKE_BUYER_FREIGHT", "1")
    calls = []
    get_map = {"100500123": _probe_product_raw_skus("100500123", [("14:771", 5)])}
    client = _ProbeClient(
        _probe_search_result(["100500123"]), get_map, {}, calls, buyer_map={}
    )
    r = _run_probe_via_cookies(app_client, monkeypatch, client, "PH_CODE", "PH_STATE")
    body = r.data.decode()
    assert r.status_code == 200
    assert "Smoke de frete: desativado (investigação encerrada)" in body
    assert client.buyer_calls == []
    assert client.buyer_get_calls == []
    assert client.calls == []
    assert "PH_CODE" not in body
    assert "PH_STATE" not in body
    assert OAUTH_CODE_COOKIE not in _cookie_names(app_client)
    assert OAUTH_STATE_COOKIE not in _cookie_names(app_client)


def test_callback_complete_recovers_code_and_state_from_cookies(
    app_client, monkeypatch, caplog
):
    import logging

    monkeypatch.setenv("ALIEXPRESS_SMOKE_BUYER_FREIGHT", "1")
    monkeypatch.setenv("ALIEXPRESS_SMOKE_SEND_GOODS_COUNTRY_CODE", "CN")
    # Opt-in explícito: teste do caminho Buyer voyage no /callback/complete.
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes._freight_smoke_enabled", lambda: True
    )
    calls = []
    get_map = {"100500123": _probe_product_raw_skus("100500123", [("14:771", 5)])}
    buyer = {"100500123": _probe_buyer_success()}
    client = _ProbeClient(
        _probe_search_result(["100500123"]), get_map, {}, calls, buyer_map=buyer
    )
    code = "PH_CODE"
    state = "PH_STATE"
    with caplog.at_level(logging.INFO):
        r = _run_probe_via_cookies(
            app_client,
            monkeypatch,
            client,
            code,
            state,
            path="/integrations/aliexpress/callback/complete",
        )
    body = r.data.decode()
    assert r.status_code == 200
    assert "Buyer freight.calculate: success" in body
    # Cookies apagados após processar o callback (caminho limpo /complete).
    assert OAUTH_CODE_COOKIE not in _cookie_names(app_client)
    assert OAUTH_STATE_COOKIE not in _cookie_names(app_client)
    # Nenhuma saída de log contém a query, o code ou o state (placeholders).
    assert "?code=" not in caplog.text
    assert "&state=" not in caplog.text
    assert code not in caplog.text
    assert state not in caplog.text


def test_callback_complete_unquotes_cookie_code(app_client, monkeypatch):
    calls = []
    get_map = {"100500123": _probe_product_detail("100500123", [])}
    client = _ProbeClient(_probe_search_result(["100500123"]), get_map, {}, calls)
    captured = {}

    def _fake_exchange(*a, **kw):
        captured["code"] = a[2] if len(a) > 2 else kw.get("code")
        return _fake_token()

    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.exchange_authorization_code",
        _fake_exchange,
    )
    monkeypatch.setattr(
        "loja.integrations.aliexpress.routes.AliExpressClient",
        lambda *a, **k: client,
    )
    code_raw = "ABC+def%2FXYZ"
    state = "ST_COMPLETE"
    with app_client.session_transaction() as sess:
        sess[STATE_KEY] = {"state": state, "ts": time.time()}
    # O Node grava encodeURIComponent(valor); aqui espelhamos com quote.
    app_client.set_cookie(
        OAUTH_CODE_COOKIE, quote(code_raw, safe=""), path=OAUTH_CALLBACK_COOKIE_PATH
    )
    app_client.set_cookie(OAUTH_STATE_COOKIE, state, path=OAUTH_CALLBACK_COOKIE_PATH)
    r = app_client.get("/integrations/aliexpress/callback/complete")
    assert r.status_code == 200
    # unquote recupera o valor original exatamente (encodeURIComponent/unquote).
    assert captured.get("code") == code_raw


def test_callback_complete_removes_cookies_on_error(app_client):
    # Cookies presentes mas SEM state de sessão -> erro 400 E cookies apagados.
    app_client.set_cookie(OAUTH_CODE_COOKIE, "PH_CODE", path=OAUTH_CALLBACK_COOKIE_PATH)
    app_client.set_cookie(OAUTH_STATE_COOKIE, "PH_STATE", path=OAUTH_CALLBACK_COOKIE_PATH)
    r = app_client.get("/integrations/aliexpress/callback/complete")
    assert r.status_code == 400
    assert OAUTH_CODE_COOKIE not in _cookie_names(app_client)
    assert OAUTH_STATE_COOKIE not in _cookie_names(app_client)


def test_callback_complete_clean_reaches_flask(app_client):
    r = app_client.get("/integrations/aliexpress/callback/complete")
    assert r.status_code == 400
    assert "Falha de validação de estado OAuth" in r.data.decode()


def test_local_callback_query_string_still_works(app_client, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("ALIEXPRESS_SMOKE_BUYER_FREIGHT", "1")
    monkeypatch.setenv("ALIEXPRESS_SMOKE_SEND_GOODS_COUNTRY_CODE", "CN")
    calls = []
    get_map = {"100500123": _probe_product_raw_skus("100500123", [("14:771", 5)])}
    buyer = {"100500123": _probe_buyer_success()}
    client = _ProbeClient(
        _probe_search_result(["100500123"]), get_map, {}, calls, buyer_map=buyer
    )
    with caplog.at_level(logging.INFO):
        body = _run_probe(app_client, monkeypatch, client)
    assert "Buyer freight.calculate: success" in body
    assert "?code=" not in caplog.text
    assert "&state=" not in caplog.text


def _fake_http_response(status: int, content_type: str, body: str):
    import requests

    resp = requests.models.Response()
    resp.status_code = status
    resp.headers["Content-Type"] = content_type
    resp._content = body.encode("utf-8")
    return resp


def test_oauth_transport_http_error_sanitized(monkeypatch):
    import requests as req

    url = "https://api-sg.aliexpress.com/rest/auth/token/create"
    secret = "APPSECRET_LEAK"
    monkeypatch.setattr(
        req, "post", lambda *a, **k: _fake_http_response(500, "application/json", "{}")
    )
    with pytest.raises(TransportError) as excinfo:
        RequestsOAuthTransport().post(url, {"app_key": "k", "code": "C", "sign": secret})
    msg = str(excinfo.value)
    assert "500" in msg
    assert "host=api-sg.aliexpress.com" in msg
    assert "path=/rest/auth/token/create" in msg
    # Nenhum segredo/parâmetro vaza no erro.
    assert secret not in msg
    assert "sign" not in msg
