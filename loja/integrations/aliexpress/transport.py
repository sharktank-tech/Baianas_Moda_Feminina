"""Transporte HTTP da integração AliExpress (isolado e mockável)."""

from __future__ import annotations

from typing import Any, Iterable, Protocol
from urllib.parse import urlparse

import requests

from .errors import AliExpressTransportError


class TransportError(Exception):
    """Erro interno do transporte (não expõe secrets/params/body)."""


class TransportTimeout(TransportError):
    pass


class Transport(Protocol):
    def post(self, params: dict[str, str]) -> str:
        """Envia `params` e retorna o corpo da resposta como texto."""
        ...


def _sanitized_location(base_url: str) -> tuple[str, str]:
    parsed = urlparse(base_url)
    return parsed.hostname or "", parsed.path or "/"


def _response_length(resp: "requests.Response") -> int:
    try:
        return len(resp.content)
    except Exception:
        return len(resp.text or "")


class RequestsTransport:
    """Transporte real baseado em `requests`. Timeout explícito e obrigatório.

    Erros de transporte carregam apenas metadados sanitizados (host, path,
    http_status, content_type, response_length). Nunca incluem app_key,
    app_secret, access_token/session, sign, parâmetros completos ou body.
    """

    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        self.base_url = base_url
        self.timeout = timeout

    def post(self, params: dict[str, str]) -> str:
        try:
            resp = requests.post(
                self.base_url,
                data=params,
                timeout=self.timeout,
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
            )
        except requests.Timeout as exc:
            raise TransportTimeout(f"timeout após {self.timeout}s") from exc
        except requests.RequestException as exc:
            host, path = _sanitized_location(self.base_url)
            raise TransportError(
                f"falha de rede: {type(exc).__name__} host={host} path={path}"
            ) from exc

        host, path = _sanitized_location(self.base_url)
        content_type = resp.headers.get("Content-Type", "")
        length = _response_length(resp)

        if resp.status_code != 200:
            raise TransportError(
                f"HTTP {resp.status_code} host={host} path={path} "
                f"content_type={content_type} response_length={length}"
            )
        # Distinguir HTTP 200 com HTML/texto (ex.: bloqueio de WAF) de JSON.
        # Aceita JSON mesmo com Content-Type atípico (começa com '{').
        _body = resp.text or ""
        _looks_json = "json" in content_type.lower() or _body.lstrip().startswith("{")
        if not _looks_json:
            raise TransportError(
                f"HTTP 200 inesperado (não-JSON) host={host} path={path} "
                f"content_type={content_type} response_length={length}"
            )
        return resp.text


__all__: Iterable[str] = ["Transport", "TransportError", "TransportTimeout", "RequestsTransport"]
