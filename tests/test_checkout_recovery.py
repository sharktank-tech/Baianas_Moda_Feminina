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
    skip_single_checkout_recovery,
)


NOW = datetime(2026, 9, 14, 12, 0, 0)


@pytest.fixture(autouse=True)
def _clean_dashboard_singletons(db):
    """Ações redirecionam ao dashboard, que cria singletons no 1º acesso."""
    from loja.modules.models import ContactInfo, FooterConfig, SiteConfig

    known = {
        model: {row[0] for row in db.session.query(model.id).all()}
        for model in (SiteConfig, FooterConfig, ContactInfo)
    }
    yield
    for model, ids in known.items():
        for row in model.query.all():
            if row.id not in ids:
                db.session.delete(row)
    db.session.commit()


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


def test_find_recoverable_reservation_window_follows_config(db, app, monkeypatch):
    """A janela usa STOCK_RESERVATION_HOURS (produção opera com 4)."""
    monkeypatch.setitem(app.config, "STOCK_RESERVATION_HOURS", "4")

    in_window = _order(
        status="pending_payment",
        created_at=NOW - timedelta(hours=3),
    )
    outside_window = _order(
        status="pending_payment",
        created_at=NOW - timedelta(hours=5),
    )
    db.session.add_all([in_window, outside_window])
    db.session.commit()

    rows = find_recoverable_orders(after_hours=2, now=NOW)
    ids = {row.id for row in rows}
    assert in_window.id in ids
    assert outside_window.id not in ids


def test_find_recoverable_reservation_window_defaults_to_24h(db, app, monkeypatch):
    monkeypatch.setitem(app.config, "STOCK_RESERVATION_HOURS", "24")

    older = _order(
        status="pending_payment",
        created_at=NOW - timedelta(hours=5),
    )
    db.session.add(older)
    db.session.commit()

    rows = find_recoverable_orders(after_hours=2, now=NOW)
    assert older.id in {row.id for row in rows}


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
    response = client.get("/admin/painel/checkout-recovery")
    assert response.status_code == 200
    assert "Fila de recuperação" in response.get_data(as_text=True)
    assert "Lote administrativo desligado" in response.get_data(as_text=True)


def test_recovery_route_anonymous_redirect(client):
    response = client.get("/admin/painel/checkout-recovery")
    assert response.status_code in (302, 303)


def test_recovery_route_non_admin_denied(client, regular_user, login):
    login(regular_user.email, "senha123")
    response = client.get("/admin/painel/checkout-recovery")
    assert response.status_code in (302, 303)


def test_recovery_post_honors_gate(client, admin_user, login, db, monkeypatch):
    monkeypatch.delenv("CHECKOUT_RECOVERY_ENABLED", raising=False)
    login(admin_user.email, "senha123")
    db.session.add(_order(status="pending_payment", created_at=NOW - timedelta(hours=4)))
    db.session.commit()
    response = client.post("/admin/recuperacao-checkout", follow_redirects=True)
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "Lote desligado" in text

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


def test_individual_route_is_idempotent(
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

    order = _order(
        user_id=admin_user.id,
        customer_email="idempotent@example.test",
        created_at=utcnow() - timedelta(hours=3),
    )
    db.session.add(order)
    db.session.commit()
    login(admin_user.email, "senha123")

    first = client.post(
        f"/admin/recuperacao-checkout/{order.id}/enviar",
        follow_redirects=False,
    )
    second = client.post(
        f"/admin/recuperacao-checkout/{order.id}/enviar",
        follow_redirects=False,
    )

    assert first.status_code == 302
    assert second.status_code == 302
    assert sent == ["idempotent@example.test"]
    db.session.refresh(order)
    assert order.recovery_email_sent_at is not None


def test_individual_route_denies_anonymous_without_mutation(client, db):
    from loja.utils import utcnow

    order = _order(created_at=utcnow() - timedelta(hours=3))
    db.session.add(order)
    db.session.commit()

    response = client.post(
        f"/admin/recuperacao-checkout/{order.id}/enviar",
        follow_redirects=False,
    )

    assert response.status_code in (302, 303, 401, 403)
    db.session.refresh(order)
    assert order.recovery_email_sent_at is None


@pytest.mark.parametrize(
    ("server", "sender", "password", "port"),
    [
        ("", "sender@example.test", "secret", 587),
        ("smtp.example.test", "", "secret", 587),
        ("smtp.example.test", "sender@example.test", "", 587),
        ("smtp.example.test", "sender@example.test", "secret", ""),
        ("smtp.example.test", "sender@example.test", "secret", "abc"),
        ("smtp.example.test", "sender@example.test", "secret", 0),
        ("smtp.example.test", "sender@example.test", "secret", 65536),
    ],
)
def test_email_delivery_preflight_rejects_incomplete_or_invalid_config(
    monkeypatch, server, sender, password, port
):
    from config import Config
    from loja.modules.enviar_email import email_delivery_ready

    monkeypatch.setattr(Config, "EMAIL_SERVER", server)
    monkeypatch.setattr(Config, "EMAIL_SENDER", sender)
    monkeypatch.setattr(Config, "EMAIL_PASSWORD", password)
    monkeypatch.setattr(Config, "EMAIL_PORT", port)

    assert email_delivery_ready() is False


def test_email_delivery_preflight_accepts_complete_config(monkeypatch):
    from config import Config
    from loja.modules.enviar_email import email_delivery_ready

    monkeypatch.setattr(Config, "EMAIL_SERVER", "smtp.example.test")
    monkeypatch.setattr(Config, "EMAIL_SENDER", "sender@example.test")
    monkeypatch.setattr(Config, "EMAIL_PASSWORD", "secret")
    monkeypatch.setattr(Config, "EMAIL_PORT", "587")

    assert email_delivery_ready() is True


def test_admin_page_keeps_batch_form_hidden_by_default(
    client, admin_user, login, db, monkeypatch
):
    from loja.utils import utcnow
    import loja.modules.enviar_email as email_module

    monkeypatch.setenv("CHECKOUT_RECOVERY_ENABLED", "1")
    monkeypatch.delenv("CHECKOUT_RECOVERY_BATCH_ENABLED", raising=False)
    monkeypatch.setattr(email_module, "email_delivery_ready", lambda: True)
    order = _order(
        user_id=admin_user.id,
        created_at=utcnow() - timedelta(hours=3),
    )
    db.session.add(order)
    db.session.commit()
    login(admin_user.email, "senha123")

    response = client.get("/admin/painel/checkout-recovery")
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert f"/admin/recuperacao-checkout/{order.id}/enviar" in text
    assert 'name="csrf_token"' in text
    assert "DESLIGADO (fail-closed). Exige CHECKOUT_RECOVERY_BATCH_ENABLED=1" in text
    assert 'action="/admin/recuperacao-checkout"' not in text


def test_find_recoverable_orders_excludes_admin_skipped(db):
    skipped = _order(
        created_at=NOW - timedelta(hours=4),
        recovery_email_skipped_at=NOW - timedelta(minutes=5),
        recovery_email_skip_reason="admin_decision",
    )
    db.session.add(skipped)
    db.session.commit()

    rows = find_recoverable_orders(after_hours=2, now=NOW)

    assert skipped.id not in {row.id for row in rows}


def test_skip_single_candidate_is_audited_without_side_effects(
    db, admin_user
):
    from loja.utils import utcnow

    order = _order(
        user_id=admin_user.id,
        created_at=utcnow() - timedelta(hours=3),
    )
    original_status = order.status
    original_reserved = order.stock_reserved
    db.session.add(order)
    db.session.commit()

    skipped = skip_single_checkout_recovery(
        order.id,
        admin_user_id=admin_user.id,
    )

    assert skipped is True
    db.session.refresh(order)
    assert order.recovery_email_skipped_at is not None
    assert order.recovery_email_skipped_by_user_id == admin_user.id
    assert order.recovery_email_skip_reason == "admin_decision"
    assert order.recovery_email_sent_at is None
    assert order.status == original_status
    assert order.stock_reserved is original_reserved


def test_skip_single_candidate_is_idempotent(db, admin_user):
    from loja.utils import utcnow

    order = _order(
        user_id=admin_user.id,
        created_at=utcnow() - timedelta(hours=3),
    )
    db.session.add(order)
    db.session.commit()

    first = skip_single_checkout_recovery(
        order.id,
        admin_user_id=admin_user.id,
    )
    first_timestamp = order.recovery_email_skipped_at
    second = skip_single_checkout_recovery(
        order.id,
        admin_user_id=admin_user.id,
    )

    assert first is True
    assert second is False
    db.session.refresh(order)
    assert order.recovery_email_skipped_at == first_timestamp


def test_skip_route_requires_explicit_confirmation(
    client, admin_user, login, db
):
    from loja.utils import utcnow

    order = _order(
        user_id=admin_user.id,
        created_at=utcnow() - timedelta(hours=3),
    )
    db.session.add(order)
    db.session.commit()
    login(admin_user.email, "senha123")

    response = client.post(
        f"/admin/recuperacao-checkout/{order.id}/ignorar",
        data={},
        follow_redirects=False,
    )

    assert response.status_code == 302
    db.session.refresh(order)
    assert order.recovery_email_skipped_at is None
    assert order.recovery_email_sent_at is None


def test_skip_route_discards_only_selected_without_email_or_stock_change(
    client, admin_user, login, db, monkeypatch
):
    from loja.utils import utcnow
    import loja.services.checkout_recovery as recovery

    monkeypatch.delenv("CHECKOUT_RECOVERY_ENABLED", raising=False)

    def _unexpected_email(*args, **kwargs):
        raise AssertionError("email must not be sent while dismissing")

    monkeypatch.setattr(recovery, "enviar_email", _unexpected_email)
    selected = _order(
        user_id=admin_user.id,
        created_at=utcnow() - timedelta(hours=3),
    )
    untouched = _order(
        user_id=admin_user.id,
        created_at=utcnow() - timedelta(hours=3),
    )
    db.session.add_all([selected, untouched])
    db.session.commit()
    login(admin_user.email, "senha123")

    response = client.post(
        f"/admin/recuperacao-checkout/{selected.id}/ignorar",
        data={"confirm_skip": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    db.session.refresh(selected)
    db.session.refresh(untouched)
    assert selected.recovery_email_skipped_at is not None
    assert selected.recovery_email_sent_at is None
    assert selected.status == "pending_payment"
    assert selected.stock_reserved is True
    assert untouched.recovery_email_skipped_at is None
    assert untouched.stock_reserved is True


def test_skip_route_denies_anonymous_without_mutation(client, db):
    from loja.utils import utcnow

    order = _order(created_at=utcnow() - timedelta(hours=3))
    db.session.add(order)
    db.session.commit()

    response = client.post(
        f"/admin/recuperacao-checkout/{order.id}/ignorar",
        data={"confirm_skip": "1"},
        follow_redirects=False,
    )

    assert response.status_code in (302, 303, 401, 403)
    db.session.refresh(order)
    assert order.recovery_email_skipped_at is None


def test_skip_route_denies_non_admin_without_mutation(
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
        f"/admin/recuperacao-checkout/{order.id}/ignorar",
        data={"confirm_skip": "1"},
        follow_redirects=False,
    )

    assert response.status_code in (302, 303, 401, 403)
    db.session.refresh(order)
    assert order.recovery_email_skipped_at is None


def test_admin_page_exposes_confirmed_skip_even_when_email_gate_is_off(
    client, admin_user, login, db, monkeypatch
):
    from loja.utils import utcnow

    monkeypatch.delenv("CHECKOUT_RECOVERY_ENABLED", raising=False)
    order = _order(
        user_id=admin_user.id,
        created_at=utcnow() - timedelta(hours=3),
    )
    db.session.add(order)
    db.session.commit()
    login(admin_user.email, "senha123")

    response = client.get("/admin/painel/checkout-recovery")
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert f"/admin/recuperacao-checkout/{order.id}/ignorar" in text
    assert 'name="confirm_skip" value="1"' in text
    assert "Ignorar lembrete" in text
    assert "pedido, o pagamento e a reserva não serão alterados" in text
