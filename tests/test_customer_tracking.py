from types import SimpleNamespace

from loja.services.customer_tracking import build_customer_tracking


def _remote(position, code=None, carrier=None):
    return SimpleNamespace(
        position=position,
        tracking_code=code,
        carrier=carrier,
        tracking_updated_at=None,
        remote_order_id="MUST_NOT_LEAK",
        attempt_key="MUST_NOT_LEAK",
    )


def test_customer_projection_preserves_remote_order_positions():
    order = SimpleNamespace(
        aliexpress_orders=[
            SimpleNamespace(
                state="shipped",
                created_at=None,
                remote_orders=[
                    _remote(1, "XYZ99", "C2"),
                    _remote(0, "ABC12", "C1"),
                ],
            )
        ]
    )
    rows = build_customer_tracking(order)
    assert [row.tracking_code for row in rows] == ["ABC12", "XYZ99"]
    assert all(row.status_label == "Em trânsito" for row in rows)


def test_customer_projection_keeps_pending_tracking_explicit():
    order = SimpleNamespace(
        aliexpress_orders=[
            SimpleNamespace(
                state="confirmed",
                created_at=None,
                remote_orders=[_remote(0)],
            )
        ]
    )
    row = build_customer_tracking(order)[0]
    assert row.status_label == "Envio confirmado"
    assert row.tracking_code is None
    assert row.carrier is None


def test_fulfillment_without_remote_identity_is_visible_without_secret():
    order = SimpleNamespace(
        aliexpress_orders=[
            SimpleNamespace(
                state="pending_reconciliation",
                created_at=None,
                remote_orders=[],
            )
        ]
    )
    row = build_customer_tracking(order)[0]
    assert row.status_label == "Confirmando pedido"
    assert row.tracking_code is None


def test_projection_does_not_contain_supplier_identity_or_diagnostics():
    order = SimpleNamespace(
        aliexpress_orders=[
            SimpleNamespace(
                state="shipped",
                created_at=None,
                ali_order_id="MUST_NOT_LEAK",
                attempt_key="MUST_NOT_LEAK",
                last_error="MUST_NOT_LEAK",
                remote_orders=[_remote(0, "ABC12", "C1")],
            )
        ]
    )
    row = build_customer_tracking(order)[0]
    assert not hasattr(row, "remote_order_id")
    assert not hasattr(row, "attempt_key")
    assert not hasattr(row, "last_error")


def test_order_without_aliexpress_fulfillment_returns_empty_tuple():
    assert build_customer_tracking(SimpleNamespace(aliexpress_orders=[])) == ()
