"""Fluxo OAuth AliExpress Open Platform (protocolo SEPARADO do DS migrado).

Dois protocolos distintos (não misturar):
  - Open Platform (OAuth): https://api-sg.aliexpress.com/rest
      assinatura: HMAC-SHA256, material = API path + params ordenados
  - DS (migrado /sync): https://api-sg.aliexpress.com/sync
      assinatura: HMAC-SHA256 (sem prefixo de path), material = params ordenados

Aqui NÃO usamos sign_top() do TOP nem sign_sync() do DS. O signer deste
módulo é independente (sign_open_platform_request).
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlencode, urlparse

_logger = logging.getLogger(__name__)

from .errors import (
    AliExpressAPIError,
    AliExpressAuthError,
    AliExpressConfigError,
    AliExpressTransportError,
)
from .transport import TransportError, TransportTimeout

DEFAULT_OAUTH_AUTHORIZE_URL = "https://api-sg.aliexpress.com/oauth/authorize"
DEFAULT_OAUTH_API_BASE_URL = "https://api-sg.aliexpress.com/rest"
DEFAULT_OAUTH_API_PATH = "/auth/token/create"


# --------------------------------------------------------------------------
# Transporte OAuth (POST para URL completa, diferente do transporte TOP)
# --------------------------------------------------------------------------
class OAuthTransport:
    def post(self, url: str, params: dict) -> str:
        ...


class RequestsOAuthTransport:
    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def post(self, url: str, params: dict) -> str:
        import requests

        try:
            resp = requests.post(
                url,
                data=params,
                timeout=self.timeout,
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
            )
        except requests.Timeout as exc:
            raise TransportTimeout(f"timeout após {self.timeout}s") from exc
        except requests.RequestException as exc:
            _p = urlparse(url)
            raise TransportError(
                f"falha de rede: {type(exc).__name__} host={_p.hostname} path={_p.path or '/'}"
            ) from exc

        _p = urlparse(url)
        content_type = resp.headers.get("Content-Type", "")
        try:
            _len = len(resp.content)
        except Exception:
            _len = len(resp.text or "")
        if resp.status_code != 200:
            raise TransportError(
                f"HTTP {resp.status_code} host={_p.hostname} path={_p.path or '/'} "
                f"content_type={content_type} response_length={_len}"
            )
        _body = resp.text or ""
        _looks_json = "json" in content_type.lower() or _body.lstrip().startswith("{")
        if not _looks_json:
            raise TransportError(
                f"HTTP 200 inesperado (não-JSON) host={_p.hostname} path={_p.path or '/'} "
                f"content_type={content_type} response_length={_len}"
            )
        return resp.text


# --------------------------------------------------------------------------
# Signer Open Platform (HMAC-SHA256, path incluído no material)
# --------------------------------------------------------------------------
def sign_open_platform_request(path: str, params: Mapping[str, str], secret: str) -> str:
    """Assina uma requisição Open Platform.

    Material = path + params ordenados por nome (ASCII) concatenando nome+valor.
    `sign` é excluído do material. HMAC-SHA256 com app_secret -> hex uppercase.
    """
    if not secret:
        raise AliExpressConfigError("app_secret ausente para assinatura OAuth")
    ordered = "".join(f"{k}{params[k]}" for k in sorted(params) if k != "sign")
    material = path + ordered
    return _hmac.new(
        secret.encode("utf-8"), material.encode("utf-8"), hashlib.sha256
    ).hexdigest().upper()


def _now_open_platform_timestamp() -> str:
    # Open Platform exige Unix epoch em milissegundos (ex.: 1517820392000).
    return str(int(time.time() * 1000))


# --------------------------------------------------------------------------
# Authorization URL
# --------------------------------------------------------------------------
def build_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    authorize_url: str = DEFAULT_OAUTH_AUTHORIZE_URL,
    force_auth: bool = True,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "force_auth": "true" if force_auth else "false",
        "state": state,
    }
    return f"{authorize_url}?{urlencode(params)}"


def generate_oauth_state() -> str:
    return secrets.token_urlsafe(32)


# --------------------------------------------------------------------------
# Troca do code por access_token
# --------------------------------------------------------------------------
def exchange_authorization_code(
    app_key: str,
    app_secret: str,
    code: str,
    *,
    base_url: str = DEFAULT_OAUTH_API_BASE_URL,
    transport: Optional[OAuthTransport] = None,
) -> "AliExpressOAuthToken":
    if not app_key or not app_secret:
        raise AliExpressConfigError("app_key/app_secret obrigatórios para OAuth")
    if not code:
        raise AliExpressConfigError("authorization code ausente")

    transport = transport or RequestsOAuthTransport()
    params = {
        "app_key": app_key,
        "code": code,
        "timestamp": _now_open_platform_timestamp(),
        "sign_method": "sha256",
    }
    params["sign"] = sign_open_platform_request(DEFAULT_OAUTH_API_PATH, params, app_secret)
    url = base_url.rstrip("/") + DEFAULT_OAUTH_API_PATH

    try:
        body = transport.post(url, params)
    except TransportError as exc:
        raise AliExpressTransportError(str(exc)) from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AliExpressAuthError("resposta de token não é JSON válido", raw=body[:500]) from exc

    # Diagnóstico temporário e sanitizado do token exchange. NÃO registra
    # authorization code, AppKey, AppSecret, sign ou token.
    _tp = urlparse(base_url.rstrip("/") + DEFAULT_OAUTH_API_PATH)
    _top_keys = sorted(data.keys()) if isinstance(data, dict) else []
    _err_resp = data.get("error_response") if isinstance(data.get("error_response"), dict) else {}
    _src = (
        _err_resp
        or (data.get("error") if isinstance(data.get("error"), dict) else {})
        or (data if isinstance(data, dict) else {})
    )
    _logger.info(
        "AliExpress token exchange diagnostic: host=%s path=%s http_status=200 "
        "top_level_keys=%s error_code=%s error_sub_code=%s request_id=%s",
        _tp.hostname,
        _tp.path,
        _top_keys,
        _src.get("code"),
        _src.get("sub_code"),
        _src.get("request_id") or data.get("request_id"),
    )

    return normalize_oauth_token(data)


def _as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_oauth_token(data: Mapping[str, Any]) -> "AliExpressOAuthToken":
    if not isinstance(data, dict):
        raise AliExpressAuthError("resposta de token inesperada (não é objeto)", raw=data)

    if "access_token" in data:
        return AliExpressOAuthToken(
            access_token=str(data["access_token"]),
            refresh_token=data.get("refresh_token"),
            expires_in=_as_int(data.get("expires_in")),
            refresh_expires_in=_as_int(data.get("refresh_expires_in")),
            seller_id=data.get("seller_id") or data.get("ali_id"),
            user_id=data.get("user_id"),
            account_platform=data.get("account_platform"),
        )

    error_response = (
        data.get("error_response")
        if isinstance(data.get("error_response"), dict)
        else {}
    )
    error = (
        data.get("error")
        if isinstance(data.get("error"), dict)
        else {}
    )
    source = error_response or error or data

    code = source.get("code")
    msg = (
        source.get("msg")
        or source.get("message")
        or data.get("error_description")
        or "falha OAuth"
    )
    sub_code = source.get("sub_code")
    sub_msg = source.get("sub_msg")
    request_id = source.get("request_id") or data.get("request_id")

    raise AliExpressAuthError(
        f"{msg} (code={code}, sub_code={sub_code})",
        code=code,
        sub_code=sub_code,
        raw={
            "code": code,
            "sub_code": sub_code,
            "sub_msg": sub_msg,
            "request_id": request_id,
        },
    )


# --------------------------------------------------------------------------
# Modelo do token (sem secrets no repr)
# --------------------------------------------------------------------------
@dataclass
class AliExpressOAuthToken:
    access_token: str
    refresh_token: Optional[str] = None
    expires_in: Optional[int] = None
    refresh_expires_in: Optional[int] = None
    seller_id: Optional[str] = None
    user_id: Optional[str] = None
    account_platform: Optional[str] = None

    def __repr__(self) -> str:
        return (
            "AliExpressOAuthToken(access_token=***, refresh_token=***, "
            f"expires_in={self.expires_in!r}, refresh_expires_in={self.refresh_expires_in!r}, "
            f"seller_id={self.seller_id!r}, user_id={self.user_id!r}, "
            f"account_platform={self.account_platform!r})"
        )


__all__: Iterable[str] = [
    "AliExpressOAuthToken",
    "sign_open_platform_request",
    "build_authorization_url",
    "generate_oauth_state",
    "exchange_authorization_code",
    "DEFAULT_OAUTH_AUTHORIZE_URL",
    "DEFAULT_OAUTH_API_BASE_URL",
    "DEFAULT_OAUTH_API_PATH",
    "RequestsOAuthTransport",
]
