"""Provider REAL (porém gated) do AliExpress placeorder — Fase 8.0.11A/8.0.11J/8.0.11L/8.0.11M/8.0.11Q/8.0.11T/8.0.11U.

Este módulo implementa ``AliExpressOrderSubmitter`` chamando o gateway migrado
``/sync`` via ``sync_placeorder``. Ele é EXPLICITAMENTE DESABILITADO por padrão:

  * ``get_default_order_submitter()`` continua retornando ``HardOffOrderSubmitter``
    (inalterado);
  * só a factory explícita ``get_real_order_submitter()`` devolve o provider
    real, e ainda assim ele exige o gate ``ALIEXPRESS_PLACEORDER_ENABLED == "1"``
    exato para NÃO falhar fechado.

O gate é re-checado DENTRO de ``submit()`` antes de qualquer resolução de
credencial, DTO, assinatura ou HTTP.

Classificação conservadora do resultado:
  * 1+ ids confirmados -> AliExpressSubmissionResult(submitted);
  * 0 ids              -> AliExpressSubmissionUnknown(placeorder_success_without_order_id);
  * ids duplicados     -> AliExpressSubmissionUnknown(placeorder_remote_identity_ambiguous);
  * is_success=false   -> confirmed failure SOMENTE se determinístico (allowlist),
                          caso contrário Unknown(placeorder_remote_error);
  * timeout            -> Unknown(placeorder_timeout);
  * rede/HTTP          -> Unknown(placeorder_transport_error);
  * protocolo          -> Unknown(placeorder_protocol_error);
  * request inválido   -> Failed(placeorder_request_invalid).

FASE 8.0.11L registra ``error_code``/``error_sub_code`` somente quando passam
pelo sanitizer estrito. FASE 8.0.11M também os mantém no ContextVar sanitizado
para persistência controlada na tentativa ``unknown``; mensagem remota livre,
payload, PII, credenciais e remote order ids continuam proibidos.

FASE 8.0.11Q alinha uma lacuna específica com o exemplo oficial do TOP para
``error_response``: ``code=50`` + ``sub_code=isv.invalid-parameter``. Somente o
sub-code exato e documentado é promovido a falha confirmada; ``code=50`` por si
só continua NÃO sendo prova suficiente e permanece ``unknown``.

FASE 8.0.11T adiciona outra evidência oficial estrita de rejeição no gateway:
``code=29`` + ``sub_code=isv.appkey-not-exists``. O par exato significa AppKey
inválida/não reconhecida no TOP e é tratado como falha confirmada. ``code=29``
isolado, o sub-code isolado ou qualquer combinação diferente continuam
conservadoramente ``unknown``.

FASE 8.0.11U migra SOMENTE o transporte real de criação para o mesmo gateway
Open Platform ``https://api-sg.aliexpress.com/sync`` já usado pelos fluxos DS do
projeto. O DTO, a classificação, o snapshot, o hard-off e a política zero-retry
permanecem inalterados.
"""

from __future__ import annotations

import os
from time import perf_counter
from typing import TYPE_CHECKING, Callable, Optional

from .credential_store import (
    CredentialExpired,
    CredentialInvalid,
    CredentialNotFound,
    CredentialNotConfigured,
    get_fulfillment_access_token,
)
from .errors import AliExpressAPIError, AliExpressConfigError
from .placeorder_diagnostics import (
    log_placeorder_event,
    record_remote_error_metadata,
    record_unknown_outcome,
    request_shape,
    sanitize_unknown_code,
)
from .submission import (
    AliExpressOrderSubmitter,
    AliExpressSubmissionFailed,
    AliExpressSubmissionRequest,
    AliExpressSubmissionResult,
    AliExpressSubmissionUnknown,
    SubmissionHardOffError,
)
from .sync_placeorder import call_sync_placeorder
from .transport import TransportError, TransportTimeout

if TYPE_CHECKING:  # pragma: no cover
    from .top_placeorder import TopPlaceOrderResponse


PLACEORDER_ENABLED_ENV = "ALIEXPRESS_PLACEORDER_ENABLED"

_CONFIRMED_FAILURE_CODES = {
    "INVALID_PARAMETER",
    "PARAMETER_ERROR",
    "INVALID_SESSION",
    "SESSION_HAS_EXPIRED",
    "ISV.PARAMETER-ERROR",
    "ISV.INVALID-PARAMETER",
    "isv.parameter-error",
    "isv.invalid-permission",
}
_CONFIRMED_SUB_CODES = {
    "validation_error",
    "invalid parameter",
    "session has expired",
    "isv.invalid-parameter",
}
_CONFIRMED_GATEWAY_ERROR_PAIRS = {
    ("29", "isv.appkey-not-exists"),
}


def _placeorder_enabled() -> bool:
    return os.environ.get(PLACEORDER_ENABLED_ENV) == "1"


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1000.0, 2)


def _unknown_code(exc: AliExpressSubmissionUnknown) -> str:
    value = exc.args[0] if getattr(exc, "args", ()) else None
    return sanitize_unknown_code(value)


class TopAliExpressOrderSubmitter(AliExpressOrderSubmitter):
    """Nome preservado por compatibilidade; transporte real agora é /sync."""

    def __init__(
        self,
        *,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        resolve_access_token: Optional[Callable[[object], str]] = None,
        post: Optional[Callable[[dict], str]] = None,
    ) -> None:
        self._app_key = app_key
        self._app_secret = app_secret
        self._resolve_access_token = resolve_access_token
        self._post = post

    def _resolve_config(self) -> tuple[str, str]:
        if self._app_key and self._app_secret:
            return self._app_key, self._app_secret
        app_key = os.environ.get("ALIEXPRESS_APP_KEY")
        app_secret = os.environ.get("ALIEXPRESS_APP_SECRET")
        if not app_key or not app_secret:
            raise AliExpressConfigError(
                "ALIEXPRESS_APP_KEY/ALIEXPRESS_APP_SECRET ausentes para placeorder"
            )
        return app_key, app_secret

    def _resolved_token(self, request: "AliExpressSubmissionRequest") -> str:
        try:
            if self._resolve_access_token is not None:
                return self._resolve_access_token(request.supplier_id)
            return get_fulfillment_access_token(request.supplier_id)
        except CredentialNotConfigured:
            raise AliExpressSubmissionFailed("placeorder_credential_not_configured") from None
        except CredentialNotFound:
            raise AliExpressSubmissionFailed("placeorder_credential_not_found") from None
        except CredentialExpired:
            raise AliExpressSubmissionFailed("placeorder_credential_expired") from None
        except CredentialInvalid:
            raise AliExpressSubmissionFailed("placeorder_credential_invalid") from None

    def submit(self, request: "AliExpressSubmissionRequest") -> AliExpressSubmissionResult:
        if not _placeorder_enabled():
            raise SubmissionHardOffError("AliExpress placeorder desabilitado")
        if request is None or request.supplier_id is None:
            raise AliExpressSubmissionFailed("placeorder_request_invalid")

        shape = request_shape(request)
        log_placeorder_event("request_ready", **shape)
        started = perf_counter()
        log_placeorder_event("provider_started", **shape)

        try:
            app_key, app_secret = self._resolve_config()
            response: "TopPlaceOrderResponse" = call_sync_placeorder(
                app_key=app_key,
                app_secret=app_secret,
                access_token=self._resolved_token(request),
                request=request,
                post=self._post,
            )
        except AliExpressConfigError:
            log_placeorder_event("provider_rejected", duration_ms=_elapsed_ms(started))
            raise AliExpressSubmissionFailed("placeorder_request_invalid") from None
        except TransportTimeout:
            code = record_unknown_outcome("placeorder_timeout")
            log_placeorder_event(
                "provider_unknown", outcome_code=code, duration_ms=_elapsed_ms(started)
            )
            raise AliExpressSubmissionUnknown(code) from None
        except TransportError:
            code = record_unknown_outcome("placeorder_transport_error")
            log_placeorder_event(
                "provider_unknown", outcome_code=code, duration_ms=_elapsed_ms(started)
            )
            raise AliExpressSubmissionUnknown(code) from None
        except AliExpressAPIError:
            code = record_unknown_outcome("placeorder_protocol_error")
            log_placeorder_event(
                "provider_unknown", outcome_code=code, duration_ms=_elapsed_ms(started)
            )
            raise AliExpressSubmissionUnknown(code) from None
        except Exception:
            code = record_unknown_outcome("placeorder_unexpected_error")
            log_placeorder_event(
                "provider_unknown", outcome_code=code, duration_ms=_elapsed_ms(started)
            )
            raise

        remote_error = record_remote_error_metadata(response)
        try:
            result = _classify_placeorder(response)
        except AliExpressSubmissionUnknown as exc:
            code = record_unknown_outcome(_unknown_code(exc))
            log_placeorder_event(
                "provider_unknown",
                outcome_code=code,
                duration_ms=_elapsed_ms(started),
                is_success=bool(response.is_success),
                remote_order_count=len(response.order_ids),
                **remote_error,
            )
            raise
        except AliExpressSubmissionFailed:
            log_placeorder_event(
                "provider_rejected",
                duration_ms=_elapsed_ms(started),
                is_success=bool(response.is_success),
                remote_order_count=len(response.order_ids),
                **remote_error,
            )
            raise

        log_placeorder_event(
            "provider_confirmed",
            duration_ms=_elapsed_ms(started),
            is_success=bool(response.is_success),
            remote_order_count=len(response.order_ids),
        )
        return result


def _classify_placeorder(response: "TopPlaceOrderResponse") -> AliExpressSubmissionResult:
    if response.is_success:
        order_ids = response.order_ids
        if len(order_ids) == 0:
            raise AliExpressSubmissionUnknown("placeorder_success_without_order_id")
        if len(order_ids) != len(set(order_ids)):
            raise AliExpressSubmissionUnknown("placeorder_remote_identity_ambiguous")
        return AliExpressSubmissionResult(
            external_order_ids=order_ids,
            external_status="submitted",
        )

    if _is_confirmed_failure(response):
        return _raise_failed(response)

    raise AliExpressSubmissionUnknown("placeorder_remote_error")


def _is_confirmed_failure(response: "TopPlaceOrderResponse") -> bool:
    code = (response.error_code or "").strip()
    sub_code = (response.error_sub_code or "").strip()
    if (code, sub_code) in _CONFIRMED_GATEWAY_ERROR_PAIRS:
        return True
    if code in _CONFIRMED_FAILURE_CODES:
        return True
    if sub_code in _CONFIRMED_SUB_CODES:
        return True
    if code.isdigit() and code in {"400", "401", "402", "403", "412", "40000"}:
        return True
    return False


def _raise_failed(response: "TopPlaceOrderResponse") -> AliExpressSubmissionResult:
    code = response.error_code or response.error_sub_code or "placeorder_rejected"
    raise AliExpressSubmissionFailed(str(code))


def get_real_order_submitter(
    *,
    app_key: Optional[str] = None,
    app_secret: Optional[str] = None,
    resolve_access_token: Optional[Callable[[object], str]] = None,
    post: Optional[Callable[[dict], str]] = None,
) -> TopAliExpressOrderSubmitter:
    if not _placeorder_enabled():
        raise SubmissionHardOffError("AliExpress placeorder desabilitado")
    return TopAliExpressOrderSubmitter(
        app_key=app_key,
        app_secret=app_secret,
        resolve_access_token=resolve_access_token,
        post=post,
    )


__all__ = [
    "PLACEORDER_ENABLED_ENV",
    "TopAliExpressOrderSubmitter",
    "get_real_order_submitter",
    "_classify_placeorder",
]
