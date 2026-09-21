"""FASE 9.0.0-C — recuperação de checkout/pedido pendente (somente e-mail).

Cobre o serviço de lembrete único, o gate por env, o dedupe via
``recovery_email_sent_at`` e a rota admin de fila read-only + disparo.
Nenhum provider é consultado e nunca há tráfego pago aqui.
"""
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from loja.modules.getway_mercado_pago.payment_models import Order
from loja.services.checkout_recovery import (
    DEFAULT_AFTER_HOURS,
    find_recoverable_orders,
    is_batch_recovery_enabled,
    is_recovery_enabled,
    load_after_hours,
    run_checkout_recovery,
    send_recovery_emails,
)


NOW = datetime(2026, 9, 14, 12, 0, 0)


def _order(**overrides):
    values = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        checkout_type="product",
        status="pending_payment",
        subtotal=Decimal("49.90"),
        shipping=Decimal("10.00"),
        discount=Decimal("0.00"),
        total=Decimal("59.90"),
        stock_reserved=True,
        customer_name="Cliente",
        customer_email="cliente@example.com",
        zip_code="01310100",
        street="Rua",
        number="10",
        neighborhood="Centro",
        city="São Paulo",
        state="SP",
    )
    values.update(overrides)
    return Order(**values)


def test_gate_env_defaults_off(monkeypatch):
    monkeypatch.delenv("CHECKOUT_RECOVERY_ENABLED", raising=False)
    assert is_recovery_enabled() is False


def test_gate_env_only_exact_one(monkeypatch):
    for value in ("true", "True", "yes", "on", "0", "2", "abc"):
        monkeypatch.setenv("CHECKOUT_RECOVERY_ENABLED", value)
        assert is_recovery_enabled() is False
    monkeypatch.setenv("CHECKOUT_RECOVERY_ENABLED", "1")
    assert is_recovery_enabled() is True


def test_default_after_hours(monkeypatch):
    monkeypatch.delenv("CHECKOUT_RECOVERY_AFTER_HOURS", raising=False)
    assert load_after_hours() == DEFAULT_AFTER_HOURS


@pytest.mark.parametrize("value", [" 2", "2 ", "0", "721", "-1", "abc", "1.5"])
def test_invalid_after_hours_fails_closed(monkeypatch, value):
    monkeypatch.setenv("CHECKOUT_RECOVERY_AFTER_HOURS", value)
    with pytest.raises(ValueError, match="checkout_recovery_after_hours_invalid"):
        load_after_hours()


def test_after_hours_upper_bound_enforced(monkeypatch):
    monkeypatch.setenv("CHECKOUT_RECOVERY_AFTER_HOURS", "800")
    with pytest.raises(ValueError, match="checkout_recovery_after_hours_invalid"):
        load_after_hours()


def test_after_hours_accepts_boundary(monkeypatch):
    monkeypatch.setenv("CHECKOUT_RECOVERY_AFTER_HOURS", "720")
    assert load_after_hours() == 720


def test_find_recoverable_orders_scope(db):
    eligible = _order(status="pending_payment", created_at=NOW - timedelta(hours=4))
    paid = _order(status="paid", created_at=NOW - timedelta(hours=4))
    fresh = _order(status="pending_payment", created_at=NOW - timedelta(hours=1))
    already_sent = _order(
        status="pending_payment",
        created_at=NOW - timedelta(hours=4),
        recovery_email_sent_at=NOW - timedelta(hours=1),
    )
    no_reservation = _order(
        status="pending_payment",
        created_at=NOW - timedelta(hours=4),
        stock_reserved=False,
    )
    no_email = _order(
        status="pending_payment",
        created_at=NOW - timedelta(hours=4),
        customer_email="",
    )
    for order in (eligible, paid, fresh, already_sent, no_reservation, no_email):
        db.session.add(order)
    db.session.commit()

    rows = find_recoverable_orders(after_hours=2, now=NOW)
    ids = {row.id for row in rows}
    assert eligible.id in ids
    assert paid.id not in ids
    assert fresh.id not in ids
    assert already_sent.id not in ids
    assert no_reservation.id not in ids
    assert no_email.id not in ids


def test_find_recoverable_orders_excludes_expired_reservation(db):
    expired = _order(
        status="pending_payment",
        created_at=NOW - timedelta(hours=25),
    )
    db.session.add(expired)
    db.session.commit()

    rows = find_recoverable_orders(after_hours=2, now=NOW)
    assert expired.id not in {row.id for row in rows}


def test_find_recoverable_orders_limit(db):
    for _ in range(3):
        db.session.add(
            _order(
                status="pending_payment",
                created_at=NOW - timedelta(hours=4),
            )
        )
    db.session.commit()

    rows = find_recoverable_orders(after_hours=2, now=NOW, limit=2)
    assert len(rows) == 2


def test_find_recoverable_orders_invalid_after_hours():
    with pytest.raises(ValueError, match="checkout_recovery_after_hours_invalid"):
        find_recoverable_orders(after_hours=0)
    with pytest.raises(ValueError, match="checkout_recovery_after_hours_invalid"):
        find_recoverable_orders(after_hours="2")


    with pytest.raises(ValueError, match="checkout_recovery_limit_invalid"):
        find_recoverable_orders(after_hours=2, limit=0)


def test_send_disabled_by_gate_does_nothing(monkeypatch):
    monkeypatch.delenv("CHECKOUT_RECOVERY_ENABLED", raising=False)
    result = send_recovery_emails([_order()])
    assert result.enabled is False
    assert result.candidates == 0
    assert result.sent == 0


def test_send_marks_only_after_success(db, monkeypatch):
    monkeypatch.setenv("CHECKOUT_RECOVERY_ENABLED", "1")
    sent = []

    def _fake_send(destinatario, assunto, corpo):
        sent.append(destinatario)

    order = _order(created_at=NOW - timedelta(hours=4))
    db.session.add(order)
    db.session.commit()

    result = send_recovery_emails(
        [order],
        email_sender=_fake_send,
        order_detail_url_builder=lambda o: "https://pollo.local/conta/pedido",
    )
    db.session.commit()

    assert result.sent == 1
    assert sent == ["cliente@example.com"]
    db.session.refresh(order)
    assert order.recovery_email_sent_at is not None


def test_send_failure_keeps_dedupe_unset(db, monkeypatch):
    monkeypatch.setenv("CHECKOUT_RECOVERY_ENABLED", "1")

    def _boom(*args, **kwargs):
        raise RuntimeError("smtp down")

    order = _order(created_at=NOW - timedelta(hours=4))
    db.session.add(order)
    db.session.commit()

    result = send_recovery_emails([order], email_sender=_boom)
    db.session.commit()

    assert result.failed == 1
    db.session.refresh(order)
    assert order.recovery_email_sent_at is None


def test_send_skips_orders_without_email(db, monkeypatch):
    monkeypatch.setenv("CHECKOUT_RECOVERY_ENABLED", "1")
    order = _order(customer_email="  ", created_at=NOW - timedelta(hours=4))
    db.session.add(order)
    db.session.commit()

    result = send_recovery_emails(
        [order],
        order_detail_url_builder=lambda o: "https://pollo.local/conta/pedido",
    )
    db.session.commit()

    assert result.skipped_empty_email == 1
    assert result.sent == 0
    db.session.refresh(order)
    assert order.recovery_email_sent_at is None


def test_message_escapes_order_data(monkeypatch):
    monkeypatch.setenv("CHECKOUT_RECOVERY_ENABLED", "1")
    captured = {}

    def _fake_send(destinatario, assunto, corpo):
        captured["corpo"] = corpo

    order = _order(created_at=NOW - timedelta(hours=4))
    send_recovery_emails(
        [order],
        email_sender=_fake_send,
        order_detail_url_builder=lambda o: "https://pollo.local/conta/pedido",
    )

    assert "R$ 59.90" in captured["corpo"]
    assert "pollo.local" in captured["corpo"]
    assert str(order.id) not in captured["corpo"]
    assert "cliente@example.com" not in captured["corpo"]


def test_recovery_route_admin_get(client, admin_user, login, db):
    login(admin_user.email, "senha123")
    db.session.add(_order(status="pending_payment", created_at=NOW - timedelta(hours=4)))
    db.session.commit()
    response = client.get("/admin/recuperacao-checkout")
    assert response.status_code == 200
    assert "Recuperação de Checkout" in response.get_data(as_text=True)
    assert 'name="csrf_token"' in response.get_data(as_text=True)


def test_recovery_route_anonymous_redirect(client):
    response = client.get("/admin/recuperacao-checkout")
    assert response.status_code in (302, 303)


def test_recovery_route_non_admin_denied(client, regular_user, login):
    login(regular_user.email, "senha123")
    response = client.get("/admin/recuperacao-checkout")
    assert response.status_code in (302, 303)


def test_recovery_post_honors_gate(client, admin_user, login, db, monkeypatch):
    monkeypatch.delenv("CHECKOUT_RECOVERY_ENABLED", raising=False)
    login(admin_user.email, "senha123")
    db.session.add(_order(status="pending_payment", created_at=NOW - timedelta(hours=4)))
    db.session.commit()
    response = client.post("/admin/recuperacao-checkout")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "E-mails enviados" in text
    assert "Habilitado" in text

def test_batch_gate_defaults_off_even_when_individual_is_enabled(monkeypatch):
    monkeypatch.setenv("CHECKOUT_RECOVERY_ENABLED", "1")
    monkeypatch.delenv("CHECKOUT_RECOVERY_BATCH_ENABLED", raising=False)

    assert is_batch_recovery_enabled() is False
    result = run_checkout_recovery(email_sender=lambda *args: None)
    assert result.enabled is False
    assert result.sent == 0


def test_batch_gate_requires_exact_one(monkeypatch):
    for value in ("true", "yes", "0", "2"):
        monkeypatch.setenv("CHECKOUT_RECOVERY_BATCH_ENABLED", value)
        assert is_batch_recovery_enabled() is False
    monkeypatch.setenv("CHECKOUT_RECOVERY_BATCH_ENABLED", "1")
    assert is_batch_recovery_enabled() is True


def test_individual_route_sends_only_selected_candidate(
    client, admin_user, login, db, monkeypatch
):
    from loja.utils import utcnow
    import loja.modules.enviar_email as email_module
    import loja.services.checkout_recovery as recovery

    monkeypatch.setenv("CHECKOUT_RECOVERY_ENABLED", "1")
    monkeypatch.delenv("CHECKOUT_RECOVERY_BATCH_ENABLED", raising=False)
    monkeypatch.setattr(email_module, "email_delivery_ready", lambda: True)
    monkeypatch.setattr(recovery, "email_delivery_ready", lambda: True)
    sent = []
    monkeypatch.setattr(
        recovery,
        "enviar_email",
        lambda recipient, subject, body: sent.append(recipient),
    )

    selected = _order(
        user_id=admin_user.id,
        customer_email="selected@example.test",
        created_at=utcnow() - timedelta(hours=3),
    )
    untouched = _order(
        user_id=admin_user.id,
        customer_email="untouched@example.test",
        created_at=utcnow() - timedelta(hours=3),
    )
    db.session.add_all([selected, untouched])
    db.session.commit()
    login(admin_user.email, "senha123")

    response = client.post(
        f"/admin/recuperacao-checkout/{selected.id}/enviar",
        follow_redirects=False,
    )

    assert response.status_code == 302
    db.session.refresh(selected)
    db.session.refresh(untouched)
    assert selected.recovery_email_sent_at is not None
    assert untouched.recovery_email_sent_at is None
    assert sent == ["selected@example.test"]


def test_individual_route_fails_closed_without_smtp(
    client, admin_user, login, db, monkeypatch
):
    from loja.utils import utcnow
    import loja.modules.enviar_email as email_module
    import loja.services.checkout_recovery as recovery

    monkeypatch.setenv("CHECKOUT_RECOVERY_ENABLED", "1")
    monkeypatch.setattr(email_module, "email_delivery_ready", lambda: False)
    monkeypatch.setattr(recovery, "email_delivery_ready", lambda: False)
    order = _order(
        user_id=admin_user.id,
        created_at=utcnow() - timedelta(hours=3),
    )
    db.session.add(order)
    db.session.commit()
    login(admin_user.email, "senha123")

    response = client.post(
        f"/admin/recuperacao-checkout/{order.id}/enviar",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Entrega de e-mail indisponível" in response.get_data(as_text=True)
    db.session.refresh(order)
    assert order.recovery_email_sent_at is None


def test_individual_route_denies_non_admin(
    client, regular_user, login, db
):
    from loja.utils import utcnow

    order = _order(
        user_id=regular_user.id,
        created_at=utcnow() - timedelta(hours=3),
    )
    db.session.add(order)
    db.session.commit()
    login(regular_user.email, "senha123")

    response = client.post(
        f"/admin/recuperacao-checkout/{order.id}/enviar",
        follow_redirects=False,
    )
    assert response.status_code in (302, 303, 403)
    db.session.refresh(order)
    assert order.recovery_email_sent_at is None


def test_email_delivery_logs_never_include_recipient_or_exception(
    monkeypatch, caplog
):
    from config import Config
    import loja.modules.enviar_email as email_module

    monkeypatch.setattr(Config, "EMAIL_SERVER", "smtp.example.test")
    monkeypatch.setattr(Config, "EMAIL_PORT", 587)
    monkeypatch.setattr(Config, "EMAIL_SENDER", "sender@example.test")
    monkeypatch.setattr(Config, "EMAIL_PASSWORD", "secret-password")

    class BrokenSMTP:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("sensitive-provider-detail")

    monkeypatch.setattr(email_module.smtplib, "SMTP", BrokenSMTP)
    with pytest.raises(RuntimeError, match="email_delivery_failed"):
        email_module.enviar_email(
            "private-recipient@example.test",
            "Assunto",
            "Corpo",
        )

    output = caplog.text
    assert "private-recipient@example.test" not in output
    assert "sensitive-provider-detail" not in output
    assert "secret-password" not in output
    assert "email_delivery failed=1" in output
