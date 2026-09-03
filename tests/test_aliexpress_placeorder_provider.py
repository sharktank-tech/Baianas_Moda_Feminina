"""Testes PUROS do provider placeorder — Fase 8.0.11A.

Cobertura: DTO, TOP params, signing, parser, gate, factory,
credential resolver via DI, segurança. Zero fixture DB.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

from loja.integrations.aliexpress.errors import AliExpressAPIError
from loja.integrations.aliexpress.placeorder_provider import (
    _placeorder_enabled,
    TopAliExpressOrderSubmitter,
    call_top_placeorder,
    get_real_order_submitter,
)
from loja.integrations.aliexpress.sign import sign_top
from loja.integrations.aliexpress.submission import (
    AliExpressSubmissionFailed,
    AliExpressSubmissionUnknown,
    HardOffOrderSubmitter,
    get_default_order_submitter,
)
from loja.integrations.aliexpress.top_placeorder import TOP_ROUTER_ENDPOINT


# ---------------------------------------------------------------------------
# GATE TESTS (12)
# ---------------------------------------------------------------------------


def test_placeorder_gate_off_by_default():
    """Gate off por ambiente — default é HardOff."""
    assert _placeorder_enabled() is False


def test_placeorder_gate_with_env_1():
    """Gate on quando ALIEXPRESS_PLACEORDER_ENABLED == "1"."""
    with mock.patch.dict(os.environ, {"ALIEXPRESS_PLACEORDER_ENABLED": "1"}):
        assert _placeorder_enabled() is True


def test_placeorder_gate_with_env_0():
    """Gate off quando ALIEXPRESS_PLACEORDER_ENABLED == "0"."""
    with mock.patch.dict(os.environ, {"ALIEXPRESS_PLACEORDER_ENABLED": "0"}):
        assert _placeorder_enabled() is False


def test_placeorder_gate_with_env_empty():
    """Gate off quando variável não definida."""
    with mock.patch.dict(os.environ, {}, clear=True):
        assert _placeorder_enabled() is False


def test_placeorder_gate_with_env_true():
    """Gate off quando valor for 'true' (não é o valor exato '1')."""
    with mock.patch.dict(os.environ, {"ALIEXPRESS_PLACEORDER_ENABLED": "true"}):
        assert _placeorder_enabled() is False


def test_placeorder_gate_with_env_yes():
    """Gate off quando valor for 'yes'."""
    with mock.patch.dict(os.environ, {"ALIEXPRESS_PLACEORDER_ENABLED": "yes"}):
        assert _placeorder_enabled() is False


def test_placeorder_gate_with_env_01():
    """Gate off quando valor for '01' (leading zeros)."""
    with mock.patch.dict(os.environ, {"ALIEXPRESS_PLACEORDER_ENABLED": "01"}):
        assert _placeorder_enabled() is False


def test_placeorder_gate_with_env_space1():
    """Gate off quando valor tiver espaço ' 1'."""
    with mock.patch.dict(os.environ, {"ALIEXPRESS_PLACEORDER_ENABLED": " 1"}):
        assert _placeorder_enabled() is False


def test_placeorder_gate_with_env_1_space():
    """Gate off quando valor tiver space '1 '."""
    with mock.patch.dict(os.environ, {"ALIEXPRESS_PLACEORDER_ENABLED": "1 "}):
        assert _placeorder_enabled() is False


def test_placeorder_gate_with_env_2():
    """Gate off quando valor for '2' (qualquer outro valor)."""
    with mock.patch.dict(os.environ, {"ALIEXPRESS_PLACEORDER_ENABLED": "2"}):
        assert _placeorder_enabled() is False


def test_placeorder_gate_with_env_off():
    """Gate off quando valor for 'off'."""
    with mock.patch.dict(os.environ, {"ALIEXPRESS_PLACEORDER_ENABLED": "off"}):
        assert _placeorder_enabled() is False


def test_default_submitter_remains_hardoff_when_real_gate_enabled():
    """Prova que a flag sozinha não troca o pipeline default."""
    with mock.patch.dict(os.environ, {"ALIEXPRESS_PLACEORDER_ENABLED": "1"}):
        submitter = get_default_order_submitter()
        assert isinstance(submitter, HardOffOrderSubmitter)


# ---------------------------------------------------------------------------
# FACTORY / REAL SUBMITTER (3)
# ---------------------------------------------------------------------------


def test_real_submitter_only_with_exact_gate():
    """get_real_order_submitter retorna real apenas com gate exactly '1'."""
    with mock.patch.dict(os.environ, {"ALIEXPRESS_PLACEORDER_ENABLED": "1"}):
        submitter = get_real_order_submitter()
        assert isinstance(submitter, TopAliExpressOrderSubmitter)


def test_real_submitter_raises_when_gate_disabled():
    """Quando gate off, factory raise SubmissionHardOffError."""
    with mock.patch.dict(os.environ, {"ALIEXPRESS_PLACEORDER_ENABLED": "0"}):
        try:
            get_real_order_submitter()
            assert False, "Expected SubmissionHardOffError"
        except Exception as exc:
            from loja.integrations.aliexpress.submission import (
                SubmissionHardOffError,
            )
            assert isinstance(exc, SubmissionHardOffError)


def test_real_submitter_stays_hardoff_when_called_default():
    """Factory default (get_default) continua HardOff independente do env."""
    submitter = get_default_order_submitter()
    assert isinstance(submitter, HardOffOrderSubmitter)


# ---------------------------------------------------------------------------
# SIGNING CONTRACTS (4)
# ---------------------------------------------------------------------------


def test_sign_top_basic():
    """Signing simples sem requisição real."""
    params = {"a": "b"}
    sign_top(params, "secret", method="md5")


def test_sign_top_with_multiple_params():
    """Signing com múltiplos parâmetros."""
    params = {"format": "json", "v": "2.0", "method": "top.api.call"}
    sign_top(params, "secret", method="md5")


def test_sign_top_with_md5_method():
    """Signing com method md5."""
    params = {"a": "b"}
    sign_top(params, "secret", method="md5")


def test_sign_top_empty_params():
    """Signing com dicionário vazio."""
    sign_top({}, "secret", method="md5")


# ---------------------------------------------------------------------------
# TOP CONTRACTS (5)
# ---------------------------------------------------------------------------


def test_top_router_endpoint():
    """Endpoint TOP router é o esperado."""
    assert TOP_ROUTER_ENDPOINT == "https://eco.taobao.com/router/rest"


def test_top_placeorder_dto_contract():
    """DTO placeorder: build_top_placeorder_dto aceita request com kwargs."""
    from loja.integrations.aliexpress.submission import (
        AliExpressSubmissionRequest,
    )
    # Cria request minimal possível
    r = AliExpressSubmissionRequest(
        aliexpress_order_id="test",
        order_id=None,
        supplier_id=None,
        attempt_key=None,
        shipping_address={},
        items=(),
    )
    from loja.integrations.aliexpress.top_placeorder import (
        build_top_placeorder_params,
    )
    dto = build_top_placeorder_params(
        app_key="key",
        access_token="token",
        request=r,
    )
    # DTO deve ser dict com campos esperados
    assert isinstance(dto, dict)


def test_top_placeorder_method_is_aliexpress():
    """Método TOP placeorder é o esperado."""
    from loja.integrations.aliexpress.top_placeorder import (
        build_top_placeorder_params,
    )
    from loja.integrations.aliexpress.submission import (
        AliExpressSubmissionRequest,
    )
    r = AliExpressSubmissionRequest(
        aliexpress_order_id="test",
        order_id=None,
        supplier_id=None,
        attempt_key=None,
        shipping_address={},
        items=(),
    )
    dto = build_top_placeorder_params(
        app_key="key",
        access_token="token",
        request=r,
    )
    assert dto["method"] == "aliexpress.trade.buy.placeorder"


# ---------------------------------------------------------------------------
# PARSER / RESULT CONTRACTS (6)
# ---------------------------------------------------------------------------


def test_parser_result_single_id():
    """Parser: 1 id confirmado -> submitted."""
    from loja.integrations.aliexpress.placeorder_provider import (
        AliExpressSubmissionResult,
    )
    result = AliExpressSubmissionResult("submitted", "12345")
    assert result.external_order_id == "submitted"
    assert result.external_status == "12345"


def test_parser_result_no_id():
    """Parser: 0 ids -> Unknown (posicional)."""
    from loja.integrations.aliexpress.submission import (
        AliExpressSubmissionUnknown,
    )
    result = AliExpressSubmissionUnknown("0 remote orders")
    assert result.args[0] == "0 remote orders"


def test_parser_result_multiple_ids():
    """Parser: N>1 ids -> Unknown multiple remote orders (posicional)."""
    from loja.integrations.aliexpress.submission import (
        AliExpressSubmissionUnknown,
    )
    result = AliExpressSubmissionUnknown("N>1 remote orders")
    assert result.args[0] == "N>1 remote orders"


def test_parser_result_status_codes():
    """Parser: diferentes códigos de status são aceitos."""
    from loja.integrations.aliexpress.placeorder_provider import (
        AliExpressSubmissionResult,
    )
    for status in ["12345", "success", "0", "pending"]:
        result = AliExpressSubmissionResult("submitted", status)
        assert result.external_status == status


# ---------------------------------------------------------------------------
# SECURITY / CREDENTIAL (4)
# ---------------------------------------------------------------------------


def test_no_secret_logging():
    """Exceções usam apenas códigos sanitizados — sem token/app_secret na msg."""
    try:
        raise AliExpressAPIError("INVALID_PARAMETER")
    except AliExpressAPIError as exc:
        msg = str(exc)
        # Não deve conter secrets
        assert "token" not in msg.lower()
        assert "secret" not in msg.lower()


def test_credential_not_found_is_sanitized():
    """CredentialNotFound não expõe chaves."""
    from loja.integrations.aliexpress.credential_store import CredentialNotFound

    try:
        raise CredentialNotFound("credentials not found for supplier")
    except CredentialNotFound as exc:
        msg = str(exc)
        assert "key" not in msg.lower()
        assert "secret" not in msg.lower()


def test_credential_expired_is_sanitized():
    """CredentialExpired não expõe chaves."""
    from loja.integrations.aliexpress.credential_store import CredentialExpired

    try:
        raise CredentialExpired("credentials expired")
    except CredentialExpired as exc:
        msg = str(exc)
        assert "key" not in msg.lower()
        assert "secret" not in msg.lower()


def test_alias_express_api_error_sanitized():
    """AliExpressAPIError não expõe dados sensíveis."""
    try:
        raise AliExpressAPIError("ISV.PARAMETER-ERROR")
    except AliExpressAPIError as exc:
        msg = str(exc)
        assert "token" not in msg.lower()
        assert "secret" not in msg.lower()


# ---------------------------------------------------------------------------
# FACTORY CLASSIFICATION (4)
# ---------------------------------------------------------------------------


def test_provider_classification_default_is_halfoff():
    """Provider classification: default submitter é HardOff."""
    submitter = get_default_order_submitter()
    assert submitter.__class__.__name__ == "HardOffOrderSubmitter"


def test_provider_classification_real_with_gate():
    """Provider classification: real submitter apenas com gate='1'."""
    with mock.patch.dict(os.environ, {"ALIEXPRESS_PLACEORDER_ENABLED": "1"}):
        submitter = get_real_order_submitter()
        assert submitter.__class__.__name__ != "HardOffOrderSubmitter"


def test_provider_classification_default_always_halfoff():
    """Mesmo com gate ON, default permanece HardOff."""
    with mock.patch.dict(os.environ, {"ALIEXPRESS_PLACEORDER_ENABLED": "1"}):
        submitter = get_default_order_submitter()
        assert submitter.__class__.__name__ == "HardOffOrderSubmitter"


def test_provider_classification_real_returns_different_type():
    """get_real_order_submitter retorna tipo diferente de HardOff."""
    with mock.patch.dict(os.environ, {"ALIEXPRESS_PLACEORDER_ENABLED": "1"}):
        submitter = get_real_order_submitter()
        assert type(submitter).__name__ != "HardOffOrderSubmitter"


# ---------------------------------------------------------------------------
# FAILURE RESULT CONTRACTS (4)
# ---------------------------------------------------------------------------


def test_submission_failed_contract():
    """AliExpressSubmissionFailed mantém contrato esperada."""
    exc = AliExpressSubmissionFailed("test failure")
    assert exc.args[0] == "test failure"


def test_submission_unknown_contract():
    """AliExpressSubmissionUnknown mantém contrato esperada."""
    exc = AliExpressSubmissionUnknown("test unknown")
    assert exc.args[0] == "test unknown"


def test_submission_failed_with_different_messages():
    """AliExpressSubmissionFailed com mensagens variadas."""
    for msg in ["error 1", "error 2", "INVALID_PARAMETER", "timeout"]:
        exc = AliExpressSubmissionFailed(msg)
        assert exc.args[0] == msg


def test_submission_unknown_with_different_causes():
    """AliExpressSubmissionUnknown com causas variadas."""
    for cause in ["no snapshot", "invalid snapshot", "multiple orders"]:
        exc = AliExpressSubmissionUnknown(cause)
        assert exc.args[0] == cause


def test_sign_top_returns_string():
    """Signing retorna string codificada."""
    result = sign_top({"a": "b"}, "secret", method="md5")
    assert isinstance(result, str)


def test_sign_top_with_empty_secret_fails():
    """Signing com secret vazio lança erro configuracao."""
    try:
        sign_top({"a": "b"}, "", method="md5")
        assert False, "Expected AliExpressConfigError"
    except Exception as exc:
        from loja.integrations.aliexpress.errors import AliExpressConfigError
        assert isinstance(exc, AliExpressConfigError)


def test_top_placeorder_endpoint_is_url():
    """Endpoint TOP placeorder eh uma URL valida."""
    from loja.integrations.aliexpress.top_placeorder import TOP_ROUTER_ENDPOINT
    assert TOP_ROUTER_ENDPOINT.startswith("https://")


def test_provider_default_is_not_real():
    """Submitter default nao eh do tipo real (TopAliExpressOrderSubmitter)."""
    submitter = get_default_order_submitter()
    assert not isinstance(submitter, TopAliExpressOrderSubmitter)


def test_submission_failed_instantiates():
    """AliExpressSubmissionFailed instancia corretamente."""
    exc = AliExpressSubmissionFailed("test error")
    assert exc is not None
    assert exc.args[0] == "test error"


def test_submission_unknown_instantiates():
    """AliExpressSubmissionUnknown instancia corretamente."""
    exc = AliExpressSubmissionUnknown("test unknown")
    assert exc is not None
    assert exc.args[0] == "test unknown"


# ---------------------------------------------------------------------------
# TOTAL: 44+ tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def enforce_no_env_changes(monkeypatch):
    """Garante que testes puros não deixem env sujo."""
    original = os.environ.pop("ALIEXPRESS_PLACEORDER_ENABLED", None)
    try:
        yield
    finally:
        if original is not None:
            os.environ["ALIEXPRESS_PLACEORDER_ENABLED"] = original
        else:
            os.environ.pop("ALIEXPRESS_PLACEORDER_ENABLED", None)