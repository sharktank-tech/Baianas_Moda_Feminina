"""Provider REAL (porém gated) do AliExpress placeorder — Fase 8.0.11A/8.0.11J.

Este módulo implementa ``AliExpressOrderSubmitter`` chamando o protocolo TOP
``top_placeorder``. Ele é EXPLICITAMENTE DESABILITADO por padrão:

  * ``get_default_order_submitter()`` continua retornando ``HardOffOrderSubmitter``
    (inalterado);
  * só a factory explícita ``get_real_order_submitter()`` devolve o provider
    real, e ainda assim ele exige o gate ``ALIEXPRESS_PLACEORDER_ENABLED == "1"``
    exato para NÃO falhar fechado.

O gate é re-checado DENTRO de ``submit()`` (defense-in-depth) ANTES de qualquer
resolução de credencial, DTO, assinatura ou HTTP. Isso nos permite provar por
teste: gate off -> zero acesso a credencial e zero HTTP.

Classificação conservadora do resultado (a regra de negócio NÃO vive no parser):
  * 1+ ids confirmados -> AliExpressSubmissionResult(submitted);
  * 0 ids              -> AliExpressSubmissionUnknown(placeorder_success_without_order_id);
  * ids duplicados     -> AliExpressSubmissionUnknown(placeorder_remote_identity_ambiguous);
  * is_success=false   -> confirmed failure SOMENTE se determinístico (allowlist),
                          caso contrário Unknown(placeorder_remote_error);
  * timeout            -> Unknown(placeorder_timeout);
  * rede/HTTP          -> Unknown(placeorder_transport_error);
  * protocolo          -> Unknown(placeorder_protocol_error);
  * request inválido   -> Failed(placeorder_request_invalid).

FASE 8.0.11J adiciona telemetria estritamente sanitizada. Ela registra somente
forma/cardinalidade/duração e códigos allow-listed; nunca registra token, sign,
app_secret/key, payload, endereço, telefone ou remote order IDs.
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
from .top_placeorder import call_top_placeorder
from .transport import TransportError, TransportTimeout

if TYPE_CHECKING:  # pragma: no cover
    from .top_placeorder import TopPlaceOrderResponse


PLACEORDER_ENABLED_ENV = "ALIEXPRESS_PLACEORDER_ENABLED"

# allowlist mínima e explicitamente documentada de repetições de erro
# CONFIRMADAS (parâmetro/validação/auth explícita). Qualquer coisa fora disso,
# ou em dúvida, vira Unknown.
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
}


def _placeorder_enabled() -> bool:
    """Gate: permite o provider real SOMENTE quando o env é EXATAMENTE ``"1"``."""
    return os.environ.get(PLACEORDER_ENABLED_ENV) == "1"


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1000.0, 2)


def _unknown_code(exc: AliExpressSubmissionUnknown) -> str:
    value = exc.args[0] if getattr(exc, "args", ()) else None
    return sanitize_unknown_code(value)


class TopAliExpressOrderSubmitter(AliExpressOrderSubmitter):
    """Submitter real do placeorder via protocolo TOP.

    Recebe DI: ``app_key``/``app_secret``, ``resolve_access_token(supplier_id)``
    e ``post`` (função http injetável). NADA é resolvido no ``__init__`` — tudo
    acontece em ``submit()`` DEPOIS do gate.
    """

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
        # GATE obrigatório ANTES de tudo (defense-in-depth, além da factory).
        if not _placeorder_enabled():
            raise SubmissionHardOffError("AliExpress placeorder desabilitado")

        if request is None:
            raise AliExpressSubmissionFailed("placeorder_request_invalid")
        if request.supplier_id is None:
            raise AliExpressSubmissionFailed("placeorder_request_invalid")

        shape = request_shape(request)
        log_placeorder_event("request_ready", **shape)
        started = perf_counter()
        log_placeorder_event("provider_started", **shape)

        try:
            app_key, app_secret = self._resolve_config()
            response: "TopPlaceOrderResponse" = call_top_placeorder(
                app_key=app_key,
                app_secret=app_secret,
                access_token=self._resolved_token(request),
                request=request,
                post=self._post,
            )
        except AliExpressConfigError:
            log_placeorder_event(
                "provider_rejected",
                duration_ms=_elapsed_ms(started),
            )
            # Falha LOCAL de validação/construção ANTES do HTTP: confirmada.
            raise AliExpressSubmissionFailed("placeorder_request_invalid") from None
        except TransportTimeout:
            log_placeorder_event(
                "provider_unknown",
                outcome_code="placeorder_timeout",
                duration_ms=_elapsed_ms(started),
            )
            raise AliExpressSubmissionUnknown("placeorder_timeout") from None
        except TransportError:
            log_placeorder_event(
                "provider_unknown",
                outcome_code="placeorder_transport_error",
                duration_ms=_elapsed_ms(started),
            )
            raise AliExpressSubmissionUnknown("placeorder_transport_error") from None
        except AliExpressAPIError:
            log_placeorder_event(
                "provider_unknown",
                outcome_code="placeorder_protocol_error",
                duration_ms=_elapsed_ms(started),
            )
            raise AliExpressSubmissionUnknown("placeorder_protocol_error") from None
        except Exception:
            # Defesa adicional: não renderizar a exceção, pois mensagens de
            # bibliotecas/provedor podem conter dados sensíveis.
            log_placeorder_event(
                "provider_unknown",
                outcome_code="placeorder_unexpected_error",
                duration_ms=_elapsed_ms(started),
            )
            raise

        try:
            result = _classify_placeorder(response)
        except AliExpressSubmissionUnknown as exc:
            log_placeorder_event(
                "provider_unknown",
                outcome_code=_unknown_code(exc),
                duration_ms=_elapsed_ms(started),
                is_success=bool(response.is_success),
                remote_order_count=len(response.order_ids),
            )
            raise
        except AliExpressSubmissionFailed:
            log_placeorder_event(
                "provider_rejected",
                duration_ms=_elapsed_ms(started),
                is_success=bool(response.is_success),
                remote_order_count=len(response.order_ids),
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
    """Classificação conservadora da resposta normalizada (regra do PROVIDER).

    Preserva todos os IDs distintos retornados em sucesso. IDs duplicados são
    ambíguos e nunca são reduzidos para o primeiro elemento.
    """
    if response.is_success:
        order_ids = response.order_ids
        if len(order_ids) == 0:
            raise AliExpressSubmissionUnknown(
                "placeorder_success_without_order_id"
            )
        if len(order_ids) != len(set(order_ids)):
            raise AliExpressSubmissionUnknown(
                "placeorder_remote_identity_ambiguous"
            )
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
    if code in _CONFIRMED_FAILURE_CODES:
        return True
    if sub_code in _CONFIRMED_SUB_CODES:
        return True
    # error_response com code numérico de validação explícita (ex.: 400/402)
    # também conta como confirmado.
    if code.isdigit() and code in {"400", "401", "402", "403", "412", "40000"}:
        return True
    return False


def _raise_failed(response: "TopPlaceOrderResponse") -> AliExpressSubmissionResult:
    # Código sanitizado — nunca o texto remoto bruto.
    code = response.error_code or response.error_sub_code or "placeorder_rejected"
    raise AliExpressSubmissionFailed(str(code))


def get_real_order_submitter(
    *,
    app_key: Optional[str] = None,
    app_secret: Optional[str] = None,
    resolve_access_token: Optional[Callable[[object], str]] = None,
    post: Optional[Callable[[dict], str]] = None,
) -> TopAliExpressOrderSubmitter:
    """Factory EXPLÍCITA do provider real.

    Diferente da default (que é hard-off), esta factory é solicitada quando se
    pretende ENVIAR de verdade: portanto, se o gate estiver OFF, FALHA FECHADO
    (SubmissionHardOffError) em vez de retornar um provider inerte em silêncio.
    """
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
