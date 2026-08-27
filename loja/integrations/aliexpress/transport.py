"""Transporte HTTP da integração AliExpress (isolado e mockável)."""

from __future__ import annotations

from typing import Any, Iterable, Protocol

import requests

from .errors import AliExpressTransportError


class TransportError(Exception):
    """Erro interno do transporte (não expõe secrets)."""


class TransportTimeout(TransportError):
    pass


class Transport(Protocol):
    def post(self, params: dict[str, str]) -> str:
        """Envia `params` e retorna o corpo da resposta como texto."""
        ...


class RequestsTransport:
    """Transporte real baseado em `requests`. Timeout explícito e obrigatório."""

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
            raise TransportError(f"falha de rede: {type(exc).__name__}") from exc

        if resp.status_code != 200:
            raise TransportError(f"HTTP {resp.status_code}")
        return resp.text


__all__: Iterable[str] = ["Transport", "TransportError", "TransportTimeout", "RequestsTransport"]
