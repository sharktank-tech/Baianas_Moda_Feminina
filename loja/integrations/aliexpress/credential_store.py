"""Armazenamento persistente de credenciais AliExpress criptografadas (Fase 8.0).

OAuth exchange -> persistir access_token/refresh_token criptografados com Fernet.
NUNCA escreve em Supplier.access_token/api_secret (compatibilidade legada).

Env dedicada: ALIEXPRESS_CREDENTIAL_KEY (chave Fernet nativa, gerada por
Fernet.generate_key()). Fail-closed: env ausente/chave inválida/ciphertext
corrompido -> credencial indisponível, nunca fallback plaintext.

Nunca logar: access_token, refresh_token, ciphertext, chave Fernet.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from loja import db
from loja.modules.models import Supplier
from loja.utils import utcnow

CREDENTIAL_KEY_ENV = "ALIEXPRESS_CREDENTIAL_KEY"


class CredentialStoreError(Exception):
    """Erro do armazenamento de credenciais (códigos estáveis)."""


class CredentialNotConfigured(CredentialStoreError):
    """Env ALIEXPRESS_CREDENTIAL_KEY ausente ou chave Fernet inválida."""


class CredentialInvalid(CredentialStoreError):
    """Ciphertext corrompido/alterado (InvalidToken)."""


class CredentialNotFound(CredentialStoreError):
    """Credencial não encontrada para o supplier_id."""


class CredentialExpired(CredentialStoreError):
    """Access token expirado (sem auto-refresh nesta fase)."""


def _config_key() -> Optional[str]:
    value = (os.environ.get(CREDENTIAL_KEY_ENV) or "").strip()
    return value or None


def _try_fernet(key: Optional[str]) -> Optional[Fernet]:
    if not key:
        return None
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError):
        return None


def _fernet() -> Fernet:
    fernet = _try_fernet(_config_key())
    if fernet is None:
        raise CredentialNotConfigured(
            f"{CREDENTIAL_KEY_ENV} ausente ou chave Fernet inválida"
        )
    return fernet


def is_configured() -> bool:
    return _try_fernet(_config_key()) is not None


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _generate_idempotency_key(order_id: str, supplier_id: str) -> str:
    base = f"fulfillment:v1:{order_id}:{supplier_id}"
    return _sha256_hex(base)


class AliExpressCredential(db.Model):
    __tablename__ = "aliexpress_credentials"

    id = db.Column(
        db.dialects.postgresql.UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    supplier_id = db.Column(
        db.dialects.postgresql.UUID(as_uuid=True),
        db.ForeignKey("suppliers.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    seller_id = db.Column(db.String(64), nullable=False)
    access_token_encrypted = db.Column(db.Text, nullable=False)
    refresh_token_encrypted = db.Column(db.Text, nullable=True)
    access_token_expires_at = db.Column(db.DateTime, nullable=True)
    refresh_token_expires_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(
        db.String(20),
        nullable=False,
        default="active",
        server_default="active",
    )
    last_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        db.CheckConstraint(
            "status IN ('active', 'expired', 'invalid', 'disabled')",
            name="ck_aliexpress_credential_status",
        ),
    )

    supplier = db.relationship("Supplier", backref=db.backref("aliexpress_credential", uselist=False))

    def __repr__(self) -> str:
        return (
            f"<AliExpressCredential supplier_id={self.supplier_id} "
            f"seller_id={self.seller_id} status={self.status}>"
        )


def _decrypt_or_raise(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise CredentialInvalid("ciphertext corrompido ou chave alterada")


def persist_credential(
    *,
    supplier_id,
    seller_id: str,
    access_token: str,
    refresh_token: Optional[str] = None,
    expires_in: Optional[int] = None,
    refresh_expires_in: Optional[int] = None,
) -> AliExpressCredential:
    """Persiste/atualiza credencial criptografada após OAuth exchange.

    - Upsert por supplier_id (UNIQUE)
    - Calcula expires_at a partir de expires_in quando existir
    - Atualiza existing em vez de criar duplicata
    """
    if not is_configured():
        raise CredentialNotConfigured("ALIEXPRESS_CREDENTIAL_KEY não configurada")

    fernet = _fernet()
    now = utcnow()

    access_enc = fernet.encrypt(access_token.encode("utf-8")).decode("ascii")
    refresh_enc = (
        fernet.encrypt(refresh_token.encode("utf-8")).decode("ascii")
        if refresh_token
        else None
    )

    access_expires_at = (
        datetime.fromtimestamp(now.timestamp() + expires_in, tz=timezone.utc)
        if expires_in
        else None
    )
    refresh_expires_at = (
        datetime.fromtimestamp(now.timestamp() + refresh_expires_in, tz=timezone.utc)
        if refresh_expires_in
        else None
    )

    cred = AliExpressCredential.query.filter_by(supplier_id=supplier_id).first()
    if cred is None:
        cred = AliExpressCredential(
            supplier_id=supplier_id,
            seller_id=seller_id,
        )
        db.session.add(cred)

    cred.seller_id = seller_id
    cred.access_token_encrypted = access_enc
    cred.refresh_token_encrypted = refresh_enc
    cred.access_token_expires_at = access_expires_at
    cred.refresh_token_expires_at = refresh_expires_at
    cred.status = "active"
    cred.last_error = None
    cred.updated_at = utcnow()

    db.session.flush()
    return cred


def get_fulfillment_access_token(supplier_id) -> str:
    """Descriptografa e valida access_token para uso em fulfillment.

    - Verifica existência da credencial
    - Verifica status == active
    - Verifica expiração (access_token_expires_at)
    - Retorna token APENAS em memória
    - Levanta erro de domínio se expirado/indisponível
    - NÃO tenta refresh (fase futura)
    """
    if not is_configured():
        raise CredentialNotConfigured("ALIEXPRESS_CREDENTIAL_KEY não configurada")

    cred = AliExpressCredential.query.filter_by(supplier_id=supplier_id).first()
    if cred is None:
        raise CredentialNotFound(f"credencial não encontrada para supplier {supplier_id}")

    if cred.status != "active":
        raise CredentialInvalid(f"credencial com status inválido: {cred.status}")

    if cred.access_token_expires_at is not None:
        if utcnow() >= cred.access_token_expires_at:
            cred.status = "expired"
            cred.last_error = "access_token_expired"
            cred.updated_at = utcnow()
            db.session.flush()
            raise CredentialExpired("access token expirado")

    return _decrypt_or_raise(cred.access_token_encrypted)


def mark_credential_error(supplier_id, error_code: str) -> None:
    """Marca erro na credencial (sem expor secrets)."""
    cred = AliExpressCredential.query.filter_by(supplier_id=supplier_id).first()
    if cred:
        cred.status = "invalid" if error_code in ("credential_invalid", "decrypt_failed") else cred.status
        cred.last_error = error_code
        cred.updated_at = utcnow()
        db.session.flush()


__all__ = [
    "AliExpressCredential",
    "is_configured",
    "persist_credential",
    "get_fulfillment_access_token",
    "mark_credential_error",
    "CredentialStoreError",
    "CredentialNotConfigured",
    "CredentialInvalid",
    "CredentialNotFound",
    "CredentialExpired",
]