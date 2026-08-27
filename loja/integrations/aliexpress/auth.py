"""Credenciais e settings da integração AliExpress (sem auto-refresh de token)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .errors import AliExpressConfigError

DEFAULT_APP_KEY_ENV = "ALIEXPRESS_APP_KEY"
DEFAULT_APP_SECRET_ENV = "ALIEXPRESS_APP_SECRET"
DEFAULT_ACCESS_TOKEN_ENV = "ALIEXPRESS_ACCESS_TOKEN"


@dataclass
class AliExpressCredentials:
    """Credenciais da aplicação AliExpress Open Platform.

    - `app_key` / `app_secret`: obrigatórios.
    - `access_token`: opcional; se ausente, chamadas não-autorizadas falham em runtime.
    - Sem auto-refresh: respeitamos `expires_in` apenas para informar validade;
      quem renova o token é o fluxo OAuth externo, não este cliente.
    """

    app_key: str
    app_secret: str
    access_token: Optional[str] = None
    expires_in: Optional[int] = None
    token_obtained_at: Optional[float] = None

    @classmethod
    def from_env(
        cls,
        *,
        require_token: bool = False,
        app_key_env: str = DEFAULT_APP_KEY_ENV,
        app_secret_env: str = DEFAULT_APP_SECRET_ENV,
        access_token_env: str = DEFAULT_ACCESS_TOKEN_ENV,
    ) -> "AliExpressCredentials":
        app_key = os.environ.get(app_key_env)
        app_secret = os.environ.get(app_secret_env)
        access_token = os.environ.get(access_token_env)

        if not app_key or not app_secret:
            raise AliExpressConfigError(
                f"{app_key_env} e {app_secret_env} são obrigatórios no ambiente"
            )
        if require_token and not access_token:
            raise AliExpressConfigError(
                f"{access_token_env} obrigatório mas não encontrado no ambiente"
            )

        return cls(
            app_key=app_key,
            app_secret=app_secret,
            access_token=access_token,
        )

    def has_token(self) -> bool:
        return bool(self.access_token)

    def is_token_valid(self) -> bool:
        """True se há token e (quando há info de expiração) ainda não expirou.

        Sem info de expiração, confiamos no token manualmente fornecido.
        """
        if not self.access_token:
            return False
        if self.expires_in is None or self.token_obtained_at is None:
            return True
        return time.time() < (self.token_obtained_at + float(self.expires_in))

    def __repr__(self) -> str:
        return (
            "AliExpressCredentials(app_key=***, app_secret=***, "
            f"access_token={'***' if self.access_token else None}, "
            f"expires_in={self.expires_in!r})"
        )


__all__: Iterable[str] = ["AliExpressCredentials"]
