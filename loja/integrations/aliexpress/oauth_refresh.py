"""Renovação de credencial OAuth AliExpress (Fase 8.0.4) — boundary com HARD-OFF.

DISTINÇÃO IMPORTANTE: este módulo trata apenas da RENOVAÇÃO (refresh) de
credencial persistida. O fluxo OAuth de EXCHANGE/authorization da Open Platform
(``exchange_authorization_code``, ``generate_oauth_state``) continua no módulo
``oauth.py`` — protocolos e responsabilidades separados.

O domínio nunca fala HTTP de renovação diretamente. Toda renovação passa pelo
boundary ``AliExpressOAuthRefresher``; a implementação ativa em produção/dev é
``HardOffOAuthRefresher``, que falha de forma determinística (nenhuma chamada
externa é feita). Sucesso real só ocorre com um refresher fake injetado nos
testes. A criação real de pedidos permanece proibida nesta fase (ver
submission.py).

SEPARAÇÃO DE RESPONSABILIDADES:
    submission boundary -> pedido (AliExpressOrderSubmitter)
    OAuth boundary      -> credencial (AliExpressOAuthRefresher)
    Nenhuma renovação cria/modifica pedido externo.

CONTRATOS:
    * Renovação é síncrona e explicitamente acionada (nenhum worker/fila).
    * ``SELECT ... FOR UPDATE`` + ``populate_existing`` serializa refreshes
      concorrentes da MESMA credencial em PostgreSQL; quem perde a corrida
      reutiliza a credencial nova sem chamar o provider.
    * Idempotência: credencial já válida não chama o provider.
    * Persistência atômica (token, expiração e rotação num único commit), sem
      estado parcial.
    * Rotação: se o provider devolver novo refresh_token, persiste o novo; se
      não devolver, preserva o atual (contrato de integração).
    * Falha: nunca apaga credencial/refresh_token; persiste apenas estado e
      erro sanitizado em ``AliExpressCredential.last_error``; nunca loga ou
      persiste tokens/secrets/headers/payload OAuth.

TEMPORAL:
    Toda comparação usa ``loja.utils.utcnow`` (naive UTC), sem mistura com
    timezone-aware; expiração determinística.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from loja import db
from loja.utils import utcnow

from .credential_store import (
    AliExpressCredential,
    CredentialExpired,
    CredentialInvalid,
    CredentialNotConfigured,
    CredentialNotFound,
    _decrypt_or_raise,
    _fernet,
    get_fulfillment_access_token,
    is_configured,
)

logger = logging.getLogger(__name__)

# Códigos sanitizados (nunca contêm tokens/secrets).
OAUTH_CODES = {
    "CREDENTIAL_MISSING": "credential_missing",
    "CREDENTIAL_EXPIRED": "credential_expired",
    "CREDENTIAL_INVALID": "credential_invalid",
    "CREDENTIAL_REFRESH_REJECTED": "credential_refresh_rejected",
    "CREDENTIAL_REFRESH_UNAVAILABLE": "credential_refresh_unavailable",
}


class AliExpressOAuthRefreshError(Exception):
    """Erro da renovação OAuth (nunca contém tokens/secrets)."""


class AliExpressOAuthRefreshRejected(AliExpressOAuthRefreshError):
    """Provider rejeitou o refresh (refresh_token inválido/expirado)."""


class AliExpressOAuthRefreshUnavailable(AliExpressOAuthRefreshError):
    """Renovação indisponível (hard-off / provider fora do ar / resposta má)."""


@dataclass(frozen=True)
class AliExpressOAuthRefreshRequest:
    """Dados mínimos para renovar — apenas o necessário pra o provider."""

    supplier_id: uuid.UUID
    seller_id: str
    refresh_token: str


@dataclass(frozen=True)
class AliExpressOAuthRefreshResult:
    """Resposta do provider (fake). refresh_token None = preservar o atual."""

    access_token: str
    expires_in: Optional[int] = None
    refresh_token: Optional[str] = None
    refresh_expires_in: Optional[int] = None


@dataclass(frozen=True)
class AliExpressCredentialRefreshResult:
    """Desfecho da renovação persistida (sem payload/secrets)."""

    ok: bool
    refreshed: bool = False
    error_code: Optional[str] = None


class AliExpressOAuthRefresher(ABC):
    """Boundary de renovação OAuth: o domínio nunca fala HTTP diretamente."""

    @abstractmethod
    def refresh(
        self, request: AliExpressOAuthRefreshRequest
    ) -> AliExpressOAuthRefreshResult:
        """Renova o access token. Deve levantar
        ``AliExpressOAuthRefreshRejected``/``...Unavailable`` em falha."""


class HardOffOAuthRefresher(AliExpressOAuthRefresher):
    """Implementação ativa: NUNCA executa rede, falha determinística."""

    def refresh(
        self, request: AliExpressOAuthRefreshRequest
    ) -> AliExpressOAuthRefreshResult:
        raise AliExpressOAuthRefreshUnavailable(
            "renovação OAuth desabilitada (HARD-OFF 8.0.4): "
            "nenhuma chamada externa é realizada"
        )


def get_default_oauth_refresher() -> AliExpressOAuthRefresher:
    """Provider padrão de produção/dev — sempre hard-off, sem config ambígua."""
    return HardOffOAuthRefresher()


def _is_usable(cred: AliExpressCredential) -> bool:
    """Credencial utilizável? (status active + access_token não expirado).

    Temporal determinística e centralizada: utcnow() naive UTC.
    """
    if cred.status != "active":
        return False
    if cred.access_token_expires_at is not None:
        if utcnow() >= cred.access_token_expires_at:
            return False
    return True


def _decrypt_refresh_token(cred: AliExpressCredential, session) -> str:
    """Descriptografa refresh_token (re-lança CredentialInvalid sanitizada)."""
    if not cred.refresh_token_encrypted:
        raise CredentialNotFound(f"sem refresh token p/ supplier {cred.supplier_id}")
    try:
        return _decrypt_or_raise(cred.refresh_token_encrypted)
    except CredentialInvalid:
        # Ciphertext corrompido/tamperado: marca como invalid (estável).
        cred.status = "invalid"
        cred.last_error = OAUTH_CODES["CREDENTIAL_INVALID"]
        cred.updated_at = utcnow()
        session.flush()
        raise


def refresh_aliexpress_credential(
    supplier_id,
    refresher: Optional[AliExpressOAuthRefresher] = None,
    session=None,
) -> AliExpressCredentialRefreshResult:
    """Renova a credencial de forma síncrona, atômica e idempotente.

    Locking (PostgreSQL): ``SELECT ... FOR UPDATE`` + ``populate_existing``
    segue o padrão da Fase 8.0.3. Enquanto o lock é segurado, o provider é
    chamado e o commit é atômico (token + expiração + rotação juntos). Quem
    aguarda o lock relê o estado persistido: se outra execução já renovou,
    reutiliza a credencial nova SEM chamar o provider novamente.

    Em SQLite o FOR UPDATE é inócuo; a proteção é garantida e testada em
    PostgreSQL.
    """
    if refresher is None:
        refresher = get_default_oauth_refresher()
    session = session or db.session
    now = utcnow()

    if not is_configured():
        # Fail-closed: sem chave, nada de renovação possível.
        raise CredentialNotConfigured("ALIEXPRESS_CREDENTIAL_KEY não configurada")

    cred = (
        session.query(AliExpressCredential)
        .filter_by(supplier_id=supplier_id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if cred is None:
        raise CredentialNotFound(f"credencial não encontrada para supplier {supplier_id}")

    # Idempotência: credencial já válida não chama o provider.
    if _is_usable(cred):
        return AliExpressCredentialRefreshResult(ok=True, refreshed=False)

    if cred.refresh_token_encrypted is None:
        # Sem refresh token: continua bloqueado (credential_expired).
        return AliExpressCredentialRefreshResult(
            ok=False, error_code=OAUTH_CODES["CREDENTIAL_EXPIRED"]
        )

    refresh_token = _decrypt_refresh_token(cred, session)
    seller_id = cred.seller_id or ""

    request = AliExpressOAuthRefreshRequest(
        supplier_id=cred.supplier_id,
        seller_id=seller_id,
        refresh_token=refresh_token,
    )

    try:
        result = refresher.refresh(request)
    except AliExpressOAuthRefreshRejected:
        logger.warning(
            "aliexpress oauth: refresh rejeitado p/ %s code=%s",
            supplier_id, OAUTH_CODES["CREDENTIAL_REFRESH_REJECTED"],
        )
        # Preserva credencial e refresh_token; apenas erro sanitizado.
        cred.last_error = OAUTH_CODES["CREDENTIAL_REFRESH_REJECTED"]
        cred.updated_at = now
        session.commit()
        return AliExpressCredentialRefreshResult(
            ok=False, error_code=OAUTH_CODES["CREDENTIAL_REFRESH_REJECTED"]
        )
    except AliExpressOAuthRefreshUnavailable:
        logger.warning(
            "aliexpress oauth: refresh indisponível p/ %s code=%s",
            supplier_id, OAUTH_CODES["CREDENTIAL_REFRESH_UNAVAILABLE"],
        )
        cred.last_error = OAUTH_CODES["CREDENTIAL_REFRESH_UNAVAILABLE"]
        cred.updated_at = now
        session.commit()
        return AliExpressCredentialRefreshResult(
            ok=False, error_code=OAUTH_CODES["CREDENTIAL_REFRESH_UNAVAILABLE"]
        )
    except Exception:
        # Nunca logar a exceção (o texto do provider pode conter tokens).
        logger.error(
            "aliexpress oauth: erro interno refresh p/ %s code=%s",
            supplier_id, OAUTH_CODES["CREDENTIAL_REFRESH_UNAVAILABLE"],
        )
        cred.last_error = OAUTH_CODES["CREDENTIAL_REFRESH_UNAVAILABLE"]
        cred.updated_at = now
        session.commit()
        return AliExpressCredentialRefreshResult(
            ok=False, error_code=OAUTH_CODES["CREDENTIAL_REFRESH_UNAVAILABLE"]
        )

    if result is None or not getattr(result, "access_token", ""):
        cred.last_error = OAUTH_CODES["CREDENTIAL_REFRESH_UNAVAILABLE"]
        cred.updated_at = now
        session.commit()
        return AliExpressCredentialRefreshResult(
            ok=False, error_code=OAUTH_CODES["CREDENTIAL_REFRESH_UNAVAILABLE"]
        )

    # Persistência atômica: token + expiração + rotação num único commit.
    fernet = _fernet()
    cred.access_token_encrypted = fernet.encrypt(
        result.access_token.encode("utf-8")
    ).decode("ascii")
    cred.access_token_expires_at = (
        now + timedelta(seconds=result.expires_in)
        if result.expires_in is not None
        else None
    )
    if result.refresh_token:
        cred.refresh_token_encrypted = fernet.encrypt(
            result.refresh_token.encode("utf-8")
        ).decode("ascii")
    if result.refresh_expires_in is not None:
        cred.refresh_token_expires_at = (
            now + timedelta(seconds=result.refresh_expires_in)
        )
    cred.status = "active"
    cred.last_error = None
    cred.updated_at = now
    session.commit()
    return AliExpressCredentialRefreshResult(ok=True, refreshed=True)


def evaluate_credential_with_refresh(
    supplier_id,
    refresher: Optional[AliExpressOAuthRefresher] = None,
) -> Optional[str]:
    """Estado da credencial para o fluxo fulfillment/submission.

    Retorna None (OK) ou código sanitizado:
      * válida                          -> None
      * ausente / env não configurada   -> credential_missing
      * expirada + refresh disponível   -> tenta renovar via refresher
      * expirada sem refresh token      -> credential_expired
      * refresh rejeitado/indisponível  -> código sanitizado correspondente
      * ciphertext inválido             -> credential_invalid

    O refresh não modifica ordem/snapshot/catálogo — somente a credencial.
    """
    if refresher is None:
        refresher = get_default_oauth_refresher()
    try:
        get_fulfillment_access_token(supplier_id)
        return None
    except CredentialExpired:
        outcome = refresh_aliexpress_credential(supplier_id, refresher)
        if not outcome.ok:
            return outcome.error_code
        try:
            get_fulfillment_access_token(supplier_id)
            return None
        except CredentialExpired:
            return OAUTH_CODES["CREDENTIAL_EXPIRED"]
        except CredentialNotFound:
            return OAUTH_CODES["CREDENTIAL_MISSING"]
        except CredentialInvalid:
            return OAUTH_CODES["CREDENTIAL_INVALID"]
    except CredentialNotFound:
        return OAUTH_CODES["CREDENTIAL_MISSING"]
    except CredentialNotConfigured:
        return OAUTH_CODES["CREDENTIAL_MISSING"]
    except CredentialInvalid:
        return OAUTH_CODES["CREDENTIAL_INVALID"]


__all__ = [
    "AliExpressOAuthRefresher",
    "AliExpressOAuthRefreshRequest",
    "AliExpressOAuthRefreshResult",
    "AliExpressCredentialRefreshResult",
    "HardOffOAuthRefresher",
    "get_default_oauth_refresher",
    "refresh_aliexpress_credential",
    "evaluate_credential_with_refresh",
    "OAUTH_CODES",
    "AliExpressOAuthRefreshError",
    "AliExpressOAuthRefreshRejected",
    "AliExpressOAuthRefreshUnavailable",
]