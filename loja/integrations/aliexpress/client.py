"""Cliente HTTP da AliExpress Open Platform (protocolo TOP router/rest).

O cliente é puro (sem Flask/SQLAlchemy). O transporte é injetável para testes.
As credenciais NUNCA são logadas; o `sign` é computado localmente.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from typing import Any, Iterable, Mapping, Optional

from .auth import AliExpressCredentials
from .errors import (
    AliExpressAPIError,
    AliExpressAuthError,
    AliExpressConfigError,
    AliExpressRateLimitError,
    AliExpressTransportError,
    redact,
)
from .sign import sign_top
from .transport import RequestsTransport, Transport, TransportError, TransportTimeout

DEFAULT_BASE_URL = "https://api.aliexpress.com/router/rest"
DEFAULT_TIMEOUT = 10.0
DEFAULT_SIGN_METHOD = "hmac"
DEFAULT_SESSION_PARAM = "session"
SHANGHAI_TZ = timezone(timedelta(hours=8))

_RATE_LIMIT_CODES = {"555"}
_RATE_LIMIT_SUBSTRINGS = ("rate limit", "too many requests", "request limit")


class AliExpressClient:
    """Cliente mínimo para o gateway TOP router/rest da AliExpress.

    Parâmetros
    ----------
    app_key, app_secret: obrigatórios.
    access_token: opcional; enviado como parâmetro `session` (padrão TOP).
    base_url, timeout, sign_method, session_param: configuráveis/testáveis.
    transport: injetável; padrão `RequestsTransport`. Nunca nulo em runtime.
    """

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        access_token: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        sign_method: str = DEFAULT_SIGN_METHOD,
        session_param: str = DEFAULT_SESSION_PARAM,
        transport: Optional[Transport] = None,
    ) -> None:
        if not app_key or not app_secret:
            raise AliExpressConfigError("app_key e app_secret são obrigatórios")

        self._creds = AliExpressCredentials(
            app_key=app_key, app_secret=app_secret, access_token=access_token
        )
        self.base_url = base_url
        self.timeout = timeout
        self.sign_method = sign_method
        self.session_param = session_param
        self._transport = transport or RequestsTransport(base_url, timeout=timeout)

    # ----- helpers -------------------------------------------------------
    @property
    def credentials(self) -> AliExpressCredentials:
        return self._creds

    def with_token(self, access_token: str) -> "AliExpressClient":
        """Retorna uma cópia do cliente com outro access_token (sem mutar o original)."""
        new_creds = replace(self._creds, access_token=access_token)
        new = AliExpressClient(
            app_key=new_creds.app_key,
            app_secret=new_creds.app_secret,
            access_token=access_token,
            base_url=self.base_url,
            timeout=self.timeout,
            sign_method=self.sign_method,
            session_param=self.session_param,
            transport=self._transport,
        )
        return new

    def _now_timestamp(self) -> str:
        return datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _coerce(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (dict, list)):
            return json.dumps(value, separators=(",", ":"))
        return str(value)

    def _build_params(self, method: str, **business: Any) -> dict[str, str]:
        params: dict[str, str] = {
            "method": method,
            "app_key": self._creds.app_key,
            "timestamp": self._now_timestamp(),
            "format": "json",
            "v": "2.0",
            "sign_method": self.sign_method,
        }
        if self._creds.access_token:
            params[self.session_param] = self._creds.access_token

        for key, val in business.items():
            if val is None:
                continue
            params[key] = self._coerce(val)

        params["sign"] = sign_top(
            {k: v for k, v in params.items() if k != "sign"},
            self._creds.app_secret,
            method=self.sign_method,
        )
        return params

    # ----- chamada -------------------------------------------------------
    def call(self, method: str, **business: Any) -> Any:
        """Executa uma chamada à API e retorna o payload de resposta decodificado.

        Levanta AliExpressAPIError em erro de negócio e AliExpressTransportError
        em falha de rede/timeout. Nunca retorna None silenciosamente.
        """
        params = self._build_params(method, **business)
        try:
            raw = self._transport.post(params)
        except TransportTimeout as exc:
            raise AliExpressTransportError(f"timeout: {exc}") from exc
        except TransportError as exc:
            raise AliExpressTransportError(str(exc)) from exc

        return self._parse_response(raw, method=method)

    @staticmethod
    def _parse_response(raw: str, *, method: str) -> Any:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AliExpressAPIError(
                "resposta não é JSON válido", raw=raw[:500]
            ) from exc

        if not isinstance(data, dict):
            raise AliExpressAPIError("resposta inesperada (não é objeto)", raw=data)

        if "error_response" in data:
            err = data["error_response"] or {}
            code = err.get("code")
            sub_code = err.get("sub_code")
            msg = err.get("msg") or err.get("sub_msg") or "erro desconhecido"
            return AliExpressClient._raise_for_error(msg, code=code, sub_code=sub_code, raw=data)

        # Resposta por método: chave "<method>_response"
        resp_key = f"{method}_response"
        payload = data.get(resp_key, data)
        if isinstance(payload, dict):
            code = payload.get("code")
            if code is not None and str(code) != "0":
                sub_code = payload.get("sub_code")
                msg = payload.get("msg") or "erro de API"
                return AliExpressClient._raise_for_error(
                    msg, code=code, sub_code=sub_code, raw=data
                )
        return payload

    @staticmethod
    def _raise_for_error(msg: str, *, code: Any, sub_code: Any, raw: Any) -> Any:
        code_str = str(code) if code is not None else ""
        text = f"{msg} (code={code}, sub_code={sub_code})"
        if code_str in _RATE_LIMIT_CODES or any(
            s in msg.lower() for s in _RATE_LIMIT_SUBSTRINGS
        ):
            raise AliExpressRateLimitError(text, code=code, sub_code=sub_code, raw=raw)
        if code_str in ("401", "403", "40010", "27"):
            raise AliExpressAuthError(text, code=code, sub_code=sub_code, raw=raw)
        raise AliExpressAPIError(text, code=code, sub_code=sub_code, raw=raw)

    def __repr__(self) -> str:
        return (
            f"AliExpressClient(base_url={self.base_url!r}, "
            f"app_key=***, access_token={'***' if self._creds.access_token else None}, "
            f"sign_method={self.sign_method!r})"
        )


__all__: Iterable[str] = ["AliExpressClient", "DEFAULT_BASE_URL"]
