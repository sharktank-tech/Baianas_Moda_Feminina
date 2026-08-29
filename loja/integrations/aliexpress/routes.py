"""Rotas OAuth AliExpress (blueprint dedicado, isolado do core Flask).

 GET /integrations/aliexpress/authorize  -> gera state, redireciona p/ AliExpress
 GET /integrations/aliexpress/callback   -> valida state, troca code, faz DS smoke

Regras de segurança:
  - state obrigatório, comparado com hmac.compare_digest, consumido após uso.
  - NUNCA mostra/exibe access_token, refresh_token, app_secret, code ou sign.
  - Token usado SOMENTE em memória para o smoke read-only; não gravado em
    cookie/session/.env. Sem refresh automático.
"""

from __future__ import annotations

import hmac
import os
import time
from functools import wraps
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from flask import Blueprint, current_app, redirect, request, session

from loja.integrations.aliexpress import AliExpressClient
from loja.integrations.aliexpress.errors import (
    AliExpressAPIError,
    AliExpressAuthError,
    AliExpressConfigError,
    AliExpressTransportError,
)
from loja.integrations.aliexpress.oauth import (
    DEFAULT_OAUTH_API_BASE_URL,
    DEFAULT_OAUTH_AUTHORIZE_URL,
    build_authorization_url,
    exchange_authorization_code,
    generate_oauth_state,
)
from loja.utils.decorators import admin_required

STATE_KEY = "aliexpress_oauth_state"
STATE_MAX_AGE = 600  # 10 minutos
_url_prefix = "/integrations/aliexpress"

aliexpress_blueprint = Blueprint("aliexpress", __name__, url_prefix=_url_prefix)


def _env(name: str, default: Optional[str]) -> Optional[str]:
    return os.environ.get(name, default)


def _authorize_url_default() -> str:
    return _env("ALIEXPRESS_OAUTH_AUTHORIZE_URL", DEFAULT_OAUTH_AUTHORIZE_URL)


def _api_base_default() -> str:
    return _env("ALIEXPRESS_OAUTH_API_BASE_URL", DEFAULT_OAUTH_API_BASE_URL)


def get_aliexpress_callback_url() -> str:
    """Resolve a URL de callback OAuth sem depender de ALIEXPRESS_CALLBACK_URL.

    Precedência:
      1. ALIEXPRESS_CALLBACK_URL (explícito, sempre vence)
      2. VERCEL_ENV == "preview"  -> VERCEL_BRANCH_URL (host estável da branch)
      3. VERCEL_ENV == "production" -> VERCEL_PROJECT_PRODUCTION_URL
      4. fallback local/controlado -> VERCEL_PROJECT_PRODUCTION_URL (se houver)
      5. erro (AliExpressConfigError)

    Em produção NUNCA usamos uma URL de branch. VERCEL_BRANCH_URL não inclui o
    esquema https:// (doc Vercel); nós prefixamos.
    """
    explicit = (os.environ.get("ALIEXPRESS_CALLBACK_URL") or "").strip()
    if explicit:
        return explicit

    vercel_env = (
        os.environ.get("VERCEL_ENV") or os.environ.get("VERCEL_TARGET_ENV") or ""
    ).strip().lower()
    branch_url = (os.environ.get("VERCEL_BRANCH_URL") or "").strip()
    production_url = (os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or "").strip()

    if vercel_env == "preview":
        if branch_url:
            return f"https://{branch_url}/integrations/aliexpress/callback"
    elif vercel_env == "production":
        if production_url:
            return f"https://{production_url}/integrations/aliexpress/callback"
    else:
        # Ambiente local/controlado: usa a URL de produção se disponível.
        if production_url:
            return f"https://{production_url}/integrations/aliexpress/callback"

    raise AliExpressConfigError(
        "Callback OAuth AliExpress não configurado."
    )


def canonicalize_vercel_branch(func: Callable) -> Callable:
    """Redireciona para a branch URL estável ANTES de qualquer autenticação.

    Em Preview Vercel, o cookie de sessão (e o state OAuth) deve pertencer a um
    hostname estável. Se acessado pela deployment URL (outro host), redireciona
    para a branch URL antes de acionar admin_required / criar state. Não copia
    query strings sensíveis.
    """

    @wraps(func)
    def wrapped(*args, **kwargs):
        branch_host = (os.environ.get("VERCEL_BRANCH_URL") or "").strip()
        if (
            os.environ.get("VERCEL_ENV") == "preview"
            and branch_host
            and request.host != branch_host
        ):
            return redirect(
                f"https://{branch_host}/integrations/aliexpress/authorize"
            )
        return func(*args, **kwargs)

    return wrapped


@aliexpress_blueprint.route("/authorize", methods=["GET"])
@canonicalize_vercel_branch
@admin_required
def authorize():
    app_key = (os.environ.get("ALIEXPRESS_APP_KEY") or "").strip()
    if not app_key:
        return "AliExpress OAuth não configurado (ALIEXPRESS_APP_KEY ausente).", 500

    try:
        callback_url = get_aliexpress_callback_url()
    except AliExpressConfigError:
        return "AliExpress OAuth não configurado (callback ausente).", 500

    # Validação sanitizada: presença/comprimento apenas. Nunca expõe o valor.
    current_app.logger.info(
        "AliExpress OAuth app_key configurado: yes, length=%d", len(app_key)
    )

    state = generate_oauth_state()
    # state: geração criptográfica + uso único + expiração curta + consumo.
    session[STATE_KEY] = {"state": state, "ts": time.time()}
    url = build_authorization_url(
        client_id=app_key,
        redirect_uri=callback_url,
        state=state,
        authorize_url=_authorize_url_default(),
    )

    # Diagnóstico temporário e sanitizado (não altera o protocolo). NÃO loga
    # app_key, app_secret, state, URL completa, query string, code, token ou sign.
    _parsed_auth = urlparse(url)
    _parsed_cb = urlparse(callback_url)
    _param_names = sorted(parse_qs(_parsed_auth.query).keys())
    current_app.logger.info(
        "AliExpress OAuth redirect diagnostic: "
        "authorize_host=%s authorize_path=%s "
        "callback_scheme=%s callback_host=%s callback_path=%s "
        "app_key_length=%d params=%s",
        _parsed_auth.hostname,
        _parsed_auth.path,
        _parsed_cb.scheme,
        _parsed_cb.hostname,
        _parsed_cb.path,
        len(app_key),
        _param_names,
    )

    return redirect(url)


@aliexpress_blueprint.route("/callback", methods=["GET"])
def callback():
    code = request.args.get("code")
    state = request.args.get("state")
    # Consome imediatamente (uso único); qualquer falha abaixo rejeita.
    stored = session.pop(STATE_KEY, None)

    if not isinstance(stored, dict):
        return "Falha de validação de estado OAuth (possível CSRF).", 400
    if not state or not hmac.compare_digest(state, stored.get("state", "")):
        return "Falha de validação de estado OAuth (possível CSRF).", 400
    if time.time() - float(stored.get("ts", 0)) > STATE_MAX_AGE:
        return "Estado OAuth expirado.", 400
    if not code:
        return "Código de autorização ausente.", 400

    app_key = os.environ.get("ALIEXPRESS_APP_KEY")
    app_secret = os.environ.get("ALIEXPRESS_APP_SECRET")
    if not app_key or not app_secret:
        return "AliExpress OAuth não configurado.", 500

    # 1) Troca do code por access_token (Open Platform).
    try:
        token = exchange_authorization_code(
            app_key, app_secret, code, base_url=_api_base_default()
        )
    except (AliExpressAuthError, AliExpressTransportError) as exc:
        return _render_oauth_failure(exc)

    # 2) DS smoke read-only usando o token SOMENTE em memória.
    client = AliExpressClient(app_key=app_key, app_secret=app_secret, access_token=token.access_token)
    try:
        result = client.search_products(
            keyword="t shirt",
            locale="en_US",
            country_code="BR",
            currency="BRL",
            sort_by="orders,desc",
            page_index=1,
            page_size=5,
        )
    except (AliExpressAPIError, AliExpressTransportError) as exc:
        return _render_ds_failure(token, exc)

    return _render_success(token, result)


# --------------------------------------------------------------------------
# Renderização sanitizada (nunca tokens/code/sign)
# --------------------------------------------------------------------------
def _render_success(token: Any, result: Any) -> str:
    lines = [
        "AliExpress autorizado com sucesso",
        f"seller_id: {token.seller_id}",
        "access token: obtido",
        f"expires_in: {token.expires_in}",
        "",
        "OAuth: success",
        "DS smoke: success",
        f"gateway: {_ds_gateway()}",
        "method: aliexpress.ds.text.search",
        "sign_method: hmac",
        f"produtos recebidos: {len(result.products)}",
    ]
    return "<br>\n".join(lines)


def _render_oauth_failure(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    sub_code = getattr(exc, "sub_code", None)
    raw = getattr(exc, "raw", None)
    request_id = raw.get("request_id") if isinstance(raw, dict) else None
    # Log sanitizado: apenas a mensagem (sem code OAuth/state/token/sign).
    current_app.logger.error("AliExpress OAuth token exchange falhou: %s", getattr(exc, "message", str(exc)))
    lines = [
        "AliExpress OAuth falhou",
        "OAuth: failure",
        f"code: {code}",
        f"sub_code: {sub_code}",
        f"request_id: {request_id}",
        "DS smoke: skipped",
        "",
        "Detalhe sanitizado: verifique code/sub_code/request_id no log da aplicação.",
    ]
    return "<br>\n".join(lines), 200


def _render_ds_failure(token: Any, exc: Exception) -> str:
    code = getattr(exc, "code", None)
    sub_code = getattr(exc, "sub_code", None)
    raw = getattr(exc, "raw", None)
    request_id = _find_request_id(raw)
    lines = [
        "AliExpress autorizado, mas o smoke DS falhou",
        "OAuth: success",
        "DS smoke: failure",
        f"code: {code}",
        f"sub_code: {sub_code}",
        f"request_id: {request_id}",
        f"gateway: {_ds_gateway()}",
        "method: aliexpress.ds.text.search",
        "",
        "Detalhe sanitizado: erro registrado no log da aplicação (sem dados sensíveis).",
    ]
    return "<br>\n".join(lines), 200


def _ds_gateway() -> str:
    return "https://api.aliexpress.com/router/rest"


def _find_request_id(obj: object) -> Optional[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() == "request_id":
                return str(v)
            rid = _find_request_id(v)
            if rid:
                return rid
    if isinstance(obj, list):
        for item in obj:
            rid = _find_request_id(item)
            if rid:
                return rid
    return None
