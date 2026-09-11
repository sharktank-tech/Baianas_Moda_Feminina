"""FASE 8.0.11Q — classificação conservadora do erro oficial TOP.

Cobertura puramente local: nenhuma credencial, rede ou chamada real ao AliExpress.
"""

import pytest

from loja.integrations.aliexpress.placeorder_provider import _classify_placeorder
from loja.integrations.aliexpress.submission import (
    AliExpressSubmissionFailed,
    AliExpressSubmissionUnknown,
)
from loja.integrations.aliexpress.top_placeorder import TopPlaceOrderResponse


def _failure(*, code=None, sub_code=None):
    return TopPlaceOrderResponse(
        is_success=False,
        order_ids=(),
        error_code=code,
        error_sub_code=sub_code,
    )


def test_official_code_50_invalid_parameter_subcode_is_confirmed_failure():
    """O par do exemplo oficial é determinístico e não deve ficar unknown."""
    with pytest.raises(AliExpressSubmissionFailed) as exc:
        _classify_placeorder(
            _failure(code="50", sub_code="isv.invalid-parameter")
        )

    # A exceção continua sanitizada e usa apenas machine-code conhecido.
    assert exc.value.args[0] == "50"


def test_code_50_alone_remains_unknown():
    """Não ampliar a allowlist: code=50 sozinho continua conservador."""
    with pytest.raises(AliExpressSubmissionUnknown) as exc:
        _classify_placeorder(_failure(code="50"))

    assert exc.value.args[0] == "placeorder_remote_error"


def test_code_50_with_unrecognized_subcode_remains_unknown():
    """Somente o sub-code exato documentado ganha classificação conclusiva."""
    with pytest.raises(AliExpressSubmissionUnknown) as exc:
        _classify_placeorder(
            _failure(code="50", sub_code="isv.some-other-error")
        )

    assert exc.value.args[0] == "placeorder_remote_error"
