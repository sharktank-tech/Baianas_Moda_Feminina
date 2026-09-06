"""Regression coverage for Mercado Pago failure returns without a payment.

Checkout Pro may redirect to the failure URL with literal sentinel strings such
as ``payment_id=null`` and ``collection_id=null`` when no payment was created.
Those values must never be sent to the Mercado Pago payments API.
"""

import pytest

import loja.views.payments_routes as payments_routes


@pytest.mark.parametrize(
    ("payment_id", "collection_id"),
    [
        ("null", "null"),
        ("NULL", "null"),
        ("None", "null"),
        ("undefined", "null"),
        ("", "null"),
        (None, "null"),
    ],
)
def test_payment_return_ignores_non_payment_sentinels(
    client,
    login,
    make_user,
    monkeypatch,
    payment_id,
    collection_id,
):
    user = make_user()
    login(user.email)

    sync_calls = []
    monkeypatch.setattr(
        payments_routes,
        "_sync_payment",
        lambda value: sync_calls.append(value),
    )

    query = {}
    if payment_id is not None:
        query["payment_id"] = payment_id
    if collection_id is not None:
        query["collection_id"] = collection_id

    response = client.get(
        "/pagamentos/retorno/falha",
        query_string=query,
    )

    assert response.status_code == 200
    assert sync_calls == []


def test_payment_return_syncs_real_payment_id(
    client,
    login,
    make_user,
    monkeypatch,
):
    user = make_user()
    login(user.email)

    sync_calls = []
    monkeypatch.setattr(
        payments_routes,
        "_sync_payment",
        lambda value: sync_calls.append(value),
    )

    response = client.get(
        "/pagamentos/retorno/sucesso",
        query_string={"payment_id": "123456789"},
    )

    assert response.status_code == 200
    assert sync_calls == ["123456789"]
