"""Testes do probe CLI read-only de status DS (Fase 8.0.7A.1) — HARDEN.

Cobre o CLI ``python -m loja.integrations.aliexpress.ds_status_probe``:

1. flag ausente → exit 2, create_app NÃO chamado, DB NÃO consultado,
   adapter NÃO chamado, zero HTTP;
2. flag != "1" → mesmas garantias;
3. argumentos inválidos → exit 1, adapter não chamado;
4. supplier UUID válido mas AliExpressOrder inexistente → exit 1, adapter n/;
5. ali_order_id existe mas pertence a outro supplier → exit 1, adapter n/;
6. supplier + ali_order_id corretos → request com IDs REAIS da ordem
   (aliexpress_order_id/order_id/supplier_id/ali_order_id/attempt_key);
7. estado ready → zero adapter/HTTP;
8. pending_reconciliation → zero adapter/HTTP;
9-12. submitted/confirmed/shipped/delivered → adapter chamado exatamente uma vez;
13. adapter failure → exit 1, mensagem sanitizada, rollback executado;
14. sucesso → exit 0, output sem token/secret/raw payload;
15. entrypoint real main() dentro de app_context válido.

Nenhum teste executa rede. Adapter real é substituído por RecordingSyncer
(monkeypatch no módulo ds_order_status); o binding/estado/request usam o banco
real do app (SQLite). O arquivo usa o prefixo zz para rodar após os 4 testes
legados order-dependentes de test_aliexpress_fulfillment.py.
"""
import socket
import uuid

import pytest
from flask import has_app_context

import loja
from loja import db
from loja.integrations.aliexpress import ds_order_status as ds_mod
from loja.integrations.aliexpress.status_sync import (
    AliExpressOrderStatusResult,
    AliExpressOrderStatusSyncError,
)
from loja.modules.models import AliExpressOrder, Supplier


@pytest.fixture(autouse=True)
def _hardoff_no_network(monkeypatch):
    def _reject(*args, **kwargs):
        raise AssertionError(
            "tentativa de conexão de rede durante teste do probe 8.0.7A.1"
        )

    monkeypatch.setattr(socket.socket, "connect", _reject)
    monkeypatch.setattr(socket, "create_connection", _reject)


class RecordingSyncer:
    """Substituto do adapter: grava requests, simula resultado/erro.

    Instancia-se DENTRO do app_context do probe e registra se há contexto
    Flask válido (prova do entrypoint real).
    """

    def __init__(self, outcome="shipped", exc=None, request_id=None):
        self.outcome = outcome
        self.exc = exc
        self.request_id = request_id
        self.calls = []
        self.in_app_context = has_app_context()

    @property
    def last_request_id(self):
        return self.request_id

    def sync(self, request):
        self.calls.append(request)
        if self.exc is not None:
            raise self.exc
        return AliExpressOrderStatusResult(
            outcome=self.outcome, external_status=self.outcome
        )


def _recorder(monkeypatch, **kw):
    recorder = RecordingSyncer(**kw)
    monkeypatch.setattr(ds_mod, "AliExpressDSOrderStatusSyncer", lambda: recorder)
    return recorder


def _spy_create_app(monkeypatch):
    calls = {"n": 0, "app": None}

    def _fake():
        calls["n"] += 1
        if calls["app"] is None:
            from loja import create_app as _real

            calls["app"] = _real()
        return calls["app"]

    monkeypatch.setattr(loja, "create_app", _fake)
    return calls


def _probe_on(monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_ORDER_STATUS_PROBE", "1")


def _probe_off(monkeypatch):
    monkeypatch.delenv("ALIEXPRESS_ORDER_STATUS_PROBE", raising=False)


def _probe_env(monkeypatch, value):
    monkeypatch.setenv("ALIEXPRESS_ORDER_STATUS_PROBE", value)


def _supplier(db, **kw):
    supplier = Supplier(
        id=kw.get("id", uuid.uuid4()),
        name=kw.get("name", f"Supp Probe {uuid.uuid4().hex[:8]}"),
        channel_type=kw.get("channel_type", "aliexpress"),
        seller_id=kw.get("seller_id", f"SELLER_PROBE_{uuid.uuid4().hex[:8]}"),
        default_currency=kw.get("default_currency", "USD"),
    )
    db.session.add(supplier)
    db.session.commit()
    return supplier


def _ali_order(db, supplier, *, state="submitted", ali_order_id=None,
               order_id=None, attempt_key=None):
    ali = AliExpressOrder(
        id=uuid.uuid4(),
        supplier_id=supplier.id,
        order_id=order_id,
        ali_order_id=ali_order_id or f"PROBE-{uuid.uuid4().hex[:12]}",
        attempt_key=attempt_key or f"AK-{uuid.uuid4().hex[:12]}",
        state=state,
        ali_status=state,
        last_error=None,
        updated_at=None,
    )
    db.session.add(ali)
    db.session.commit()
    db.session.expire_all()
    return db.session.get(AliExpressOrder, ali.id)


class TestFailClosedFlag:
    @pytest.mark.parametrize("mode", ["absent", "zero", "other"])
    def test_flag_nao_habilitada_exit2_sem_app_db_http(
        self, monkeypatch, capsys, mode
    ):
        from loja.integrations.aliexpress import ds_status_probe as probe

        if mode == "absent":
            _probe_off(monkeypatch)
        elif mode == "zero":
            _probe_env(monkeypatch, "0")
        else:
            _probe_env(monkeypatch, "true")

        create_spy = _spy_create_app(monkeypatch)
        recorder = _recorder(monkeypatch)

        rc = probe.main(["6e7b0f82-9e5c-4f2b-a1c0-deadbeef0001", "PROBE-0000"])

        assert rc == 2
        assert create_spy["n"] == 0
        assert recorder.calls == []
        out = capsys.readouterr().out
        assert "PROBE fechado" in out
        assert "ALIEXPRESS_ORDER_STATUS_PROBE" in out


class TestArgumentosInvalidos:
    def test_supplier_nao_e_uuid(self, monkeypatch, capsys):
        from loja.integrations.aliexpress import ds_status_probe as probe

        _probe_on(monkeypatch)
        create_spy = _spy_create_app(monkeypatch)
        recorder = _recorder(monkeypatch)

        rc = probe.main(["nao-e-uuid", "PROBE-0001"])

        assert rc == 1
        assert create_spy["n"] == 0
        assert recorder.calls == []
        assert "failure" in capsys.readouterr().out

    def test_ali_order_id_vazio(self, monkeypatch, capsys):
        from loja.integrations.aliexpress import ds_status_probe as probe

        _probe_on(monkeypatch)
        create_spy = _spy_create_app(monkeypatch)
        recorder = _recorder(monkeypatch)

        rc = probe.main([str(uuid.uuid4()), "   "])

        assert rc == 1
        assert create_spy["n"] == 0
        assert recorder.calls == []
        assert "failure" in capsys.readouterr().out


class TestLocalBinding:
    def test_supplier_sem_ordem_correspondente(self, db, monkeypatch, capsys):
        from loja.integrations.aliexpress import ds_status_probe as probe

        _probe_on(monkeypatch)
        recorder = _recorder(monkeypatch)

        rc = probe.run_probe(str(uuid.uuid4()), "PROBE-0002")

        assert rc == 1
        assert recorder.calls == []
        out = capsys.readouterr().out
        assert "failure" in out
        assert "não encontrada" in out

    def test_ordem_de_outro_supplier(self, db, monkeypatch, capsys):
        from loja.integrations.aliexpress import ds_status_probe as probe

        _probe_on(monkeypatch)
        owner = _supplier(db)
        other = _supplier(db)
        ali = _ali_order(db, owner, ali_order_id="PROBE-OWNED-0003")

        recorder = _recorder(monkeypatch)
        rc = probe.run_probe(str(other.id), "PROBE-OWNED-0003")

        out = capsys.readouterr().out
        assert rc == 1
        assert recorder.calls == []
        assert "failure" in out
        assert str(other.id) not in out

    def test_ali_order_id_inexistente_mesmo_supplier(self, db, monkeypatch, capsys):
        from loja.integrations.aliexpress import ds_status_probe as probe

        _probe_on(monkeypatch)
        supplier = _supplier(db)
        recorder = _recorder(monkeypatch)

        rc = probe.run_probe(str(supplier.id), "PROBE-MISSING-0004")

        assert rc == 1
        assert recorder.calls == []
        assert "failure" in capsys.readouterr().out


class TestEstadoElegivel:
    @pytest.mark.parametrize(
        "state",
        [
            "queued",
            "blocked",
            "ready",
            "submitting",
            "pending_reconciliation",
            "failed",
            "manual_review",
            "pending",
            "cancel_requested",
            "cancelled",
        ],
    )
    def test_estado_inelegivel_zero_adapter(self, db, monkeypatch, capsys, state):
        from loja.integrations.aliexpress import ds_status_probe as probe

        _probe_on(monkeypatch)
        supplier = _supplier(db)
        ali = _ali_order(
            db,
            supplier,
            state=state,
            ali_order_id=f"PROBE-INELEG-{uuid.uuid4().hex[:8]}",
        )

        recorder = _recorder(monkeypatch)
        rc = probe.run_probe(str(supplier.id), ali.ali_order_id)

        assert rc == 1
        assert recorder.calls == []
        assert "não elegível" in capsys.readouterr().out


class TestEstadosElegiveisChamamAdapter:
    @pytest.mark.parametrize("state", ["submitted", "confirmed", "shipped", "delivered"])
    def test_adapter_chamado_uma_vez_com_ids_reais(
        self, db, monkeypatch, capsys, state
    ):
        from loja.integrations.aliexpress import ds_status_probe as probe

        _probe_on(monkeypatch)
        supplier = _supplier(db)
        order_id = uuid.uuid4()
        attempt_key = f"AK-REAL-{uuid.uuid4().hex[:10]}"
        ali = _ali_order(
            db,
            supplier,
            state=state,
            ali_order_id=f"PROBE-REAL-{uuid.uuid4().hex[:10]}",
            order_id=order_id,
            attempt_key=attempt_key,
        )

        recorder = _recorder(monkeypatch)
        rc = probe.run_probe(str(supplier.id), ali.ali_order_id)

        assert rc == 0
        assert len(recorder.calls) == 1
        request = recorder.calls[0]
        assert request.aliexpress_order_id == ali.id
        assert request.order_id == order_id
        assert request.supplier_id == supplier.id
        assert request.ali_order_id == ali.ali_order_id
        assert request.attempt_key == attempt_key
        out = capsys.readouterr().out
        assert "success" in out
        assert "aliexpress.ds.trade.order.get" in out
        assert "shipped" in out


class TestAdapterFailureRollback:
    def test_failure_exit1_rollback_saida_sanitizada(self, db, monkeypatch, capsys):
        from loja.integrations.aliexpress import ds_status_probe as probe

        _probe_on(monkeypatch)
        supplier = _supplier(db)
        ali = _ali_order(db, supplier, state="submitted")

        recorder = _recorder(
            monkeypatch,
            exc=AliExpressOrderStatusSyncError("falha na consulta DS (code=27)"),
        )

        rollback_calls = {"n": 0}

        def _rollback_spy(*a, **k):
            rollback_calls["n"] += 1

        monkeypatch.setattr(db.session, "rollback", _rollback_spy)

        rc = probe.run_probe(str(supplier.id), ali.ali_order_id)

        assert rc == 1
        assert len(recorder.calls) == 1
        assert rollback_calls["n"] == 1
        out = capsys.readouterr().out
        assert "failure" in out
        assert "code=27" in out
        for banned in ("token", "secret", "SUPER_SECRET", ali.ali_order_id):
            assert banned not in out


class TestSuccessOutputSanitizado:
    def test_sucesso_exit0_output_sem_segredos(self, db, monkeypatch, capsys):
        from loja.integrations.aliexpress import ds_status_probe as probe

        _probe_on(monkeypatch)
        supplier = _supplier(db)
        ali = _ali_order(db, supplier, state="shipped")

        recorder = _recorder(
            monkeypatch, outcome="delivered", request_id="REQ-777_ok"
        )
        recorder.secret_internal = "S3CRE7_DO_PROVIDER"

        rc = probe.run_probe(str(supplier.id), ali.ali_order_id)

        assert rc == 0
        out = capsys.readouterr().out
        assert "success" in out
        assert "aliexpress.ds.trade.order.get" in out
        assert "delivered" in out
        assert "REQ-777_ok" in out
        for banned in (
            "S3CRE7_DO_PROVIDER",
            "token",
            "secret",
            str(supplier.id),
            ali.ali_order_id,
            str(ali.id),
        ):
            assert banned not in out


class TestEntrypointReal:
    def test_main_usa_app_context_real(self, db, monkeypatch):
        from loja.integrations.aliexpress import ds_status_probe as probe

        _probe_on(monkeypatch)
        supplier = _supplier(db)
        ali = _ali_order(db, supplier, state="confirmed", attempt_key="AK-CLI")

        recorder = _recorder(monkeypatch)
        rc = probe.main([str(supplier.id), ali.ali_order_id])

        assert rc == 0
        assert len(recorder.calls) == 1
        assert recorder.in_app_context is True
        assert recorder.calls[0].attempt_key == "AK-CLI"
        assert recorder.calls[0].aliexpress_order_id == ali.id