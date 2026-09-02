"""Gates da Fase 8.0.8 para ativação controlada do status sync real.

Zero rede real em todos os testes. Prova que:
- ausência/valor diferente de "1" => HardOffOrderStatusSyncer;
- literal "1" => AliExpressDSOrderStatusSyncer;
- construir o adapter real não faz HTTP;
- elegibilidade local é verificada antes de syncer.sync(), portanto ready,
  pending_reconciliation e ausência de ali_order_id continuam zero HTTP.
"""

import socket
import uuid

import pytest

from loja import db
from loja.integrations.aliexpress.ds_order_status import AliExpressDSOrderStatusSyncer
from loja.integrations.aliexpress.status_sync import (
    STATUS_SYNC_ENABLE_ENV,
    HardOffOrderStatusSyncer,
    get_default_order_status_syncer,
    is_real_order_status_sync_enabled,
    sync_aliexpress_order_status,
)
from loja.modules.models import AliExpressOrder


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _reject(*args, **kwargs):
        raise AssertionError("rede proibida nos testes da Fase 8.0.8")

    monkeypatch.setattr(socket.socket, "connect", _reject)
    monkeypatch.setattr(socket, "create_connection", _reject)


@pytest.mark.parametrize("value", [None, "", "0", "true", "TRUE", "yes", "2"])
def test_flag_fail_closed(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(STATUS_SYNC_ENABLE_ENV, raising=False)
    else:
        monkeypatch.setenv(STATUS_SYNC_ENABLE_ENV, value)

    assert is_real_order_status_sync_enabled() is False
    assert isinstance(get_default_order_status_syncer(), HardOffOrderStatusSyncer)


def test_literal_um_habilita_adapter_real_sem_http(monkeypatch):
    monkeypatch.setenv(STATUS_SYNC_ENABLE_ENV, "1")

    assert is_real_order_status_sync_enabled() is True
    syncer = get_default_order_status_syncer()
    assert isinstance(syncer, AliExpressDSOrderStatusSyncer)


def test_ready_com_flag_ligada_nao_chama_provider(db, monkeypatch):
    monkeypatch.setenv(STATUS_SYNC_ENABLE_ENV, "1")

    order = AliExpressOrder(
        id=uuid.uuid4(),
        order_id=None,
        supplier_id=None,
        state="ready",
        idempotency_key=f"gate-ready-{uuid.uuid4().hex}",
        ali_order_id=None,
    )
    db.session.add(order)
    db.session.commit()

    result = sync_aliexpress_order_status(order)
    assert result.state == "ready"


def test_submitted_sem_ali_order_id_com_flag_ligada_nao_chama_provider(db, monkeypatch):
    monkeypatch.setenv(STATUS_SYNC_ENABLE_ENV, "1")

    order = AliExpressOrder(
        id=uuid.uuid4(),
        order_id=None,
        supplier_id=None,
        state="submitted",
        idempotency_key=f"gate-submitted-{uuid.uuid4().hex}",
        ali_order_id=None,
    )
    db.session.add(order)
    db.session.commit()

    result = sync_aliexpress_order_status(order)
    assert result.state == "submitted"
