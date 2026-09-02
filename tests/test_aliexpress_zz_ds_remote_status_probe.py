"""Testes do probe remote-only DS (Fase 8.0.7B). Zero rede real."""
import socket
import uuid

import pytest

import loja
from loja import db
from loja.integrations.aliexpress import ds_order_status as ds_mod
from loja.integrations.aliexpress.status_sync import (
    AliExpressOrderStatusResult,
    AliExpressOrderStatusSyncError,
)
from loja.modules.models import AliExpressOrder, Supplier


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _reject(*args, **kwargs):
        raise AssertionError("rede proibida no teste do remote status probe")
    monkeypatch.setattr(socket.socket, "connect", _reject)
    monkeypatch.setattr(socket, "create_connection", _reject)


class RecordingSyncer:
    def __init__(self, outcome="shipped", exc=None):
        self.outcome = outcome
        self.exc = exc
        self.calls = []
        self.last_request_id = "RID_TEST"

    def sync(self, request):
        self.calls.append(request)
        if self.exc:
            raise self.exc
        return AliExpressOrderStatusResult(
            outcome=self.outcome, external_status=self.outcome
        )


def _recorder(monkeypatch, **kw):
    rec = RecordingSyncer(**kw)
    monkeypatch.setattr(ds_mod, "AliExpressDSOrderStatusSyncer", lambda: rec)
    return rec


def _supplier(*, channel_type="aliexpress", seller_id="1702548795"):
    s = Supplier(
        id=uuid.uuid4(),
        name=f"Remote Probe {uuid.uuid4().hex[:8]}",
        channel_type=channel_type,
        seller_id=seller_id,
        default_currency="USD",
    )
    db.session.add(s)
    db.session.commit()
    return s


def test_flag_off_zero_app_db_http(monkeypatch, capsys):
    from loja.integrations.aliexpress import ds_remote_status_probe as probe
    monkeypatch.delenv("ALIEXPRESS_REMOTE_ORDER_STATUS_PROBE", raising=False)
    calls = {"app": 0}
    def _boom():
        calls["app"] += 1
        raise AssertionError("create_app não deveria ser chamado")
    monkeypatch.setattr(loja, "create_app", _boom)
    rc = probe.main([str(uuid.uuid4()), "1234567890123456"])
    assert rc == 2
    assert calls["app"] == 0
    assert "PROBE fechado" in capsys.readouterr().out


@pytest.mark.parametrize("supplier_id,remote_id", [
    ("x", "1234567890123456"),
    (str(uuid.uuid4()), ""),
])
def test_argumentos_invalidos_sem_app(monkeypatch, supplier_id, remote_id):
    from loja.integrations.aliexpress import ds_remote_status_probe as probe
    monkeypatch.setenv("ALIEXPRESS_REMOTE_ORDER_STATUS_PROBE", "1")
    monkeypatch.setattr(loja, "create_app", lambda: (_ for _ in ()).throw(AssertionError()))
    assert probe.run_probe(supplier_id, remote_id) == 1


def test_supplier_inexistente_zero_adapter(app, monkeypatch):
    from loja.integrations.aliexpress import ds_remote_status_probe as probe
    monkeypatch.setenv("ALIEXPRESS_REMOTE_ORDER_STATUS_PROBE", "1")
    rec = _recorder(monkeypatch)
    with app.app_context():
        rc = probe.run_probe(str(uuid.uuid4()), "1234567890123456")
    assert rc == 1
    assert rec.calls == []


def test_supplier_nao_aliexpress_zero_adapter(app, monkeypatch):
    from loja.integrations.aliexpress import ds_remote_status_probe as probe
    monkeypatch.setenv("ALIEXPRESS_REMOTE_ORDER_STATUS_PROBE", "1")
    rec = _recorder(monkeypatch)
    with app.app_context():
        s = _supplier(channel_type="manual")
        rc = probe.run_probe(str(s.id), "1234567890123456")
    assert rc == 1
    assert rec.calls == []


def test_supplier_sem_seller_id_zero_adapter(app, monkeypatch):
    from loja.integrations.aliexpress import ds_remote_status_probe as probe
    monkeypatch.setenv("ALIEXPRESS_REMOTE_ORDER_STATUS_PROBE", "1")
    rec = _recorder(monkeypatch)
    with app.app_context():
        s = _supplier(seller_id=None)
        rc = probe.run_probe(str(s.id), "1234567890123456")
    assert rc == 1
    assert rec.calls == []


def test_sucesso_uma_chamada_sem_criar_aliexpress_order(app, monkeypatch, capsys):
    from loja.integrations.aliexpress import ds_remote_status_probe as probe
    monkeypatch.setenv("ALIEXPRESS_REMOTE_ORDER_STATUS_PROBE", "1")
    rec = _recorder(monkeypatch, outcome="confirmed")
    with app.app_context():
        s = _supplier()
        before = AliExpressOrder.query.count()
        rc = probe.run_probe(str(s.id), "1234567890123456")
        after = AliExpressOrder.query.count()
    assert rc == 0
    assert len(rec.calls) == 1
    assert rec.calls[0].supplier_id == s.id
    assert rec.calls[0].ali_order_id == "1234567890123456"
    assert before == after
    out = capsys.readouterr().out
    assert "success" in out
    assert "confirmed" in out
    assert "1234567890123456" not in out
    assert str(s.id) not in out


def test_adapter_error_sanitizado(app, monkeypatch, capsys):
    from loja.integrations.aliexpress import ds_remote_status_probe as probe
    monkeypatch.setenv("ALIEXPRESS_REMOTE_ORDER_STATUS_PROBE", "1")
    rec = _recorder(monkeypatch, exc=AliExpressOrderStatusSyncError("falha sanitizada"))
    with app.app_context():
        s = _supplier()
        rc = probe.run_probe(str(s.id), "1234567890123456")
    assert rc == 1
    assert len(rec.calls) == 1
    out = capsys.readouterr().out
    assert "falha sanitizada" in out
    assert "1234567890123456" not in out
    assert str(s.id) not in out
