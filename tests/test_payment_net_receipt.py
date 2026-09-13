from decimal import Decimal
from types import SimpleNamespace

import pytest

from loja.services.payment_net_receipt import (
    PaymentNetReceiptError,
    resolve_payment_net_receipt,
)


def _payment(**overrides):
    values = {
        "provider": "mercado_pago",
        "status": "approved",
        "amount": Decimal("100.00"),
        "currency": "BRL",
        "raw_payload": {
            "transaction_amount": 100,
            "currency_id": "BRL",
            "transaction_details": {
                "net_received_amount": 94.51,
            },
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_resolves_observed_processor_retention_from_persisted_payload():
    result = resolve_payment_net_receipt(_payment())

    assert result.charged_amount == Decimal("100.00")
    assert result.net_received_amount == Decimal("94.51")
    assert result.processor_retained_amount == Decimal("5.49")
    assert result.currency == "BRL"


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "pending"},
        {"provider": "other"},
        {"raw_payload": None},
        {"raw_payload": {}},
        {
            "raw_payload": {
                "transaction_amount": "100.00",
                "currency_id": "BRL",
                "transaction_details": {},
            }
        },
    ],
)
def test_absent_or_non_final_fact_remains_unknown(overrides):
    assert resolve_payment_net_receipt(_payment(**overrides)) is None


@pytest.mark.parametrize(
    "overrides,error",
    [
        ({"amount": "99.00"}, "payment_amount_mismatch"),
        ({"currency": "USD"}, "payment_currency_mismatch"),
        (
            {
                "raw_payload": {
                    "transaction_amount": "100.00",
                    "currency_id": "BRL",
                    "transaction_details": {
                        "net_received_amount": "101.00",
                    },
                }
            },
            "payment_net_received_exceeds_charged",
        ),
        (
            {
                "raw_payload": {
                    "transaction_amount": "100.00",
                    "currency_id": "BRL",
                    "transaction_details": {
                        "net_received_amount": "NaN",
                    },
                }
            },
            "payload_net_received_amount_invalid",
        ),
    ],
)
def test_present_but_incoherent_payment_fact_fails_closed(overrides, error):
    with pytest.raises(PaymentNetReceiptError, match=error):
        resolve_payment_net_receipt(_payment(**overrides))


def test_resolver_does_not_mutate_payment_or_payload():
    payment = _payment()
    before = dict(vars(payment))
    before_payload = {
        **payment.raw_payload,
        "transaction_details": dict(payment.raw_payload["transaction_details"]),
    }

    resolve_payment_net_receipt(payment)

    assert vars(payment) == before
    assert payment.raw_payload == before_payload
