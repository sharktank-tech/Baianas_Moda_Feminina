"""Credencial administrativa temporária do catálogo AliExpress (Fase 7).

Após o OAuth bem-sucedido, o callback grava um cookie **criptografado** (não
apenas assinado) com a credencial temporária do admin. O access_token ABREVIADO
NUNCA é persistido em banco, memória de longo prazo, HTML, query string ou logs.

Segurança:
  * Fernet (AES128-CBC + HMAC-SHA256) via cryptography. ALIEXPRESS_ADMIN_SESSION_KEY
    DEVE ser uma chave Fernet nativa válida (gerada por Fernet.generate_key());
    NUNCA derivada de passphrase com SHA-256.
  * Fail-closed: sem a env OU com chave Fernet inválida, is_configured() == False
    e nenhuma rota do catálogo admin funciona (500 sanitizado / sem criação de
    sessão no callback).
  * TTL curto (~30 min) verificado no claim `exp` ao ler; sem renovação.
  * Cookie: HttpOnly, Secure, SameSite=Lax e path restrito a
    /admin/integrations/aliexpress (nunca enviado fora do catálogo admin).
  * Logs nunca contêm o payload, o ciphertext, a chave nem o access_token.

Geração operacional da chave:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

from cryptography.fernet import Fernet

from flask import Response

COOKIE_NAME = "ae_admin_session"
ADMIN_CATALOG_PATH = "/admin/integrations/aliexpress"
COOKIE_PATH = ADMIN_CATALOG_PATH
DEFAULT_TTL_SECONDS = 30 * 60


def _config_key() -> Optional[str]:
    value = (os.environ.get("ALIEXPRESS_ADMIN_SESSION_KEY") or "").strip()
    return value or None


def _try_fernet(key: Optional[str]) -> Optional[Fernet]:
    """Constrói o Fernet a partir da chave NATIVA da env. None se inválida.

    A chave é lida como está na env (Fernet key em base64-urlsafe de 32 bytes,
    como gerada por Fernet.generate_key()); sem SHA-256/passphrase.
    """
    if not key:
        return None
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError):
        return None


def is_configured() -> bool:
    """Fail-closed: funcionalidade só existe com chave Fernet válida na env."""
    return _try_fernet(_config_key()) is not None


def _fernet() -> Fernet:
    """Instância Fernet a partir da env (nunca em cache; chave nunca logada)."""
    fernet = _try_fernet(_config_key())
    if fernet is None:
        raise RuntimeError("ALIEXPRESS_ADMIN_SESSION_KEY ausente ou chave Fernet inválida")
    return fernet


def create_admin_session_value(
    *,
    seller_id: str,
    access_token: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    """Cria o value criptografado do cookie (exp verificável é incluído)."""
    now = int(time.time())
    payload = {
        "seller_id": str(seller_id or ""),
        "access_token": str(access_token or ""),
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _fernet().encrypt(plaintext).decode("ascii")


def read_admin_session(cookie_value: Optional[str]) -> Optional[dict]:
    """Decriptografa e valida o cookie. Qualquer inconsistência -> None (nunca
    levanta; access_token/exp ausentes ou expirados são tratados como inválidos)."""
    if not cookie_value:
        return None
    try:
        plaintext = _fernet().decrypt(cookie_value.encode("ascii"))
    except Exception:
        return None
    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    access_token = payload.get("access_token")
    if not isinstance(exp, int) or exp < int(time.time()):
        return None
    if not isinstance(access_token, str) or not access_token:
        return None
    seller_id = payload.get("seller_id")
    return {
        "seller_id": str(seller_id) if seller_id is not None else "",
        "access_token": access_token,
    }


def set_admin_session_cookie(
    response: Response,
    value: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> Response:
    response.set_cookie(
        COOKIE_NAME,
        value,
        max_age=int(ttl_seconds),
        httponly=True,
        secure=True,
        samesite="Lax",
        path=COOKIE_PATH,
    )
    return response


def clear_admin_session_cookie(response: Response) -> Response:
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    return response


__all__ = [
    "COOKIE_NAME",
    "ADMIN_CATALOG_PATH",
    "DEFAULT_TTL_SECONDS",
    "is_configured",
    "create_admin_session_value",
    "read_admin_session",
    "set_admin_session_cookie",
    "clear_admin_session_cookie",
]