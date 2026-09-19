"""Testes do adapter DS read-only de status AliExpress (Fase 8.0.7B) — DESABILITADO.

Garante (zero rede real):
- adapter nunca é o default (get_default_order_status_syncer() continua hard-off);
- usa EXATAMENTE aliexpress.trade.ds.order.get com
  single_order_query={"order_id": ali_order_id};
- token obtido exclusivamente via credential_store a partir do supplier_id;
- boundary request não carrega token/secret/session;
- ali_order_id inválido -> NENHUM HTTP (erro sanitizado antes da chamada);
- mapeamento remoto -> canônico (tabela completa da Fase 8.0.7A);
- precedência: evidência mais avançada apenas quando conclusiva;
- FINISH/IN_CANCEL/IN_ISSUE/IN_FROZEN/FUND_PROCESSING conservadores; NUNCA
  produz cancelled;
- erro de autenticação/rate limit/transporte/API -> exceção sanitizada (sem
  token/secret/url/body); token fake nunca aparece em mensagens;
- payload malformado/incompleto -> exceção sanitizada;
- external_status restringe-se ao canônico; payload bruto nunca é persistido;
- logistics_no/service/info_list NÃO criam Shipment/TrackingEvent;
- integração com o motor 8.0.6: confirmed/shipped/delivered avançam,
  unknown mantém estado, regressão não regride e milestones 8.0.6.1 corretos.
"""
import socket
import uuid
from decimal import Decimal

import pytest

from loja import db
from loja.integrations.aliexpress.ds_order_status import (
    AliExpressDSOrderStatusSyncer,
    DS_ORDER_STATUS_METHOD,
    map_aliexpress_ds_status,
)
from loja.integrations.aliexpress.errors import (
    AliExpressAPIError,
    AliExpressAuthError,
    AliExpressRateLimitError,
    AliExpressTransportError,
)
from loja.integrations.aliexpress.fulfillment import (
    ERROR_CODES,
    ensure_aliexpress_fulfillment,
)
from loja.integrations.aliexpress.status_sync import (
    get_default_order_status_syncer,
    HardOffOrderStatusSyncer,
    sync_aliexpress_order_status,
    AliExpressOrderStatusRequest,
    AliExpressOrderStatusSyncError,
    STATUS_CONFIRMED,
    STATUS_DELIVERED,
    STATUS_SHIPPED,
    STATUS_SUBMITTED,
    STATUS_UNKNOWN,
)
from loja.integrations.aliexpress.submission import (
    AliExpressOrderSubmitter,
    AliExpressSubmissionResult,
    submit_aliexpress_order,
)
from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem
from loja.modules.models import (
    AliExpressOrder,
    Payment,
    Product,
    ProductVariant,
    Shipment,
    Supplier,
    SupplierProduct,
    TrackingEvent,
)


def _add_approved_payment(db, order):
    """Vincula um Payment approved BRL conferindo com order.total.

    Helper explicito para testes cujo cenario representa pedido pago:
    o pagamento canonico e pre-condicao do fulfillment, nunca implicito.
    """
    db.session.add(
        Payment(
            id=uuid.uuid4(),
            order_id=order.id,
            provider_payment_id=f"mp_test_{uuid.uuid4().hex[:12]}",
            status="approved",
            amount=order.total,
            currency="BRL",
        )
    )
    db.session.commit()
from aliexpress_snapshot_utils import materialize_snapshot

ALI_ORDER_ID = "9000000000001"


@pytest.fixture(autouse=True)
def _valid_credential_env(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv(
        "ALIEXPRESS_CREDENTIAL_KEY", Fernet.generate_key().decode("utf-8")
    )


@pytest.fixture(autouse=True)
def _hardoff_no_network(monkeypatch):
    """Zero HTTP real durante toda a Fase 8.0.7B."""

    def _reject(*args, **kwargs):
        raise AssertionError(
            "tentativa de conexão de rede durante teste da Fase 8.0.7B"
        )

    monkeypatch.setattr(socket.socket, "connect", _reject)
    monkeypatch.setattr(socket, "create_connection", _reject)


class ScriptedDSClient:
    """Fake de AliExpressClient: grava chamadas e devolve envelope/erro."""

    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.calls = []

    def call(self, method, **business):
        self.calls.append((method, business))
        if self.exc is not None:
            raise self.exc
        return self.payload


class OkSubmitter(AliExpressOrderSubmitter):
    def __init__(self, external_order_id=None):
        self.calls = []
        self.external_order_id = (
            external_order_id
            if external_order_id is not None
            else str(uuid.uuid4().int)
        )

    def submit(self, request):
        self.calls.append(request)
        return AliExpressSubmissionResult(external_order_ids=(self.external_order_id,))


def _envelope(order_status=None, logistics_status=None, *, request_id=None,
              result_wrapper=False, extra=None):
    body = {}
    if order_status is not None:
        body["order_status"] = order_status
    if logistics_status is not None:
        body["logistics_status"] = logistics_status
    if extra:
        body.update(extra)
    outer = {"result": body} if result_wrapper else body
    if request_id:
        outer["request_id"] = request_id
    return outer


def _req(supplier_id, ali_order_id=ALI_ORDER_ID):
    return AliExpressOrderStatusRequest(
        aliexpress_order_id=uuid.uuid4(),
        order_id=None,
        supplier_id=supplier_id,
        ali_order_id=ali_order_id,
        attempt_key=None,
    )


class TestDefaultContinuesHardOff:
    def test_default_continua_hard_off(self):
        assert isinstance(get_default_order_status_syncer(), HardOffOrderStatusSyncer)


class TestMapDSStatus:
    """Função pura de mapeamento remoto -> canônico (sem provider/DB)."""

    @pytest.mark.parametrize(
        "order, logi, expected",
        [
            ("PLACE_ORDER_SUCCESS", None, STATUS_SUBMITTED),
            ("PAYMENT_PROCESSING", None, STATUS_SUBMITTED),
            ("RISK_CONTROL", None, STATUS_SUBMITTED),
            ("WAIT_SELLER_SEND_GOODS", None, STATUS_CONFIRMED),
            (None, "WAIT_SELLER_SEND_GOODS", STATUS_CONFIRMED),
            ("SELLER_SEND_PART_GOODS", None, STATUS_CONFIRMED),
            (None, "SELLER_SEND_PART_GOODS", STATUS_CONFIRMED),
            ("SELLER_SEND_GOODS", None, STATUS_SHIPPED),
            (None, "SELLER_SEND_GOODS", STATUS_SHIPPED),
            ("WAIT_BUYER_ACCEPT_GOODS", None, STATUS_SHIPPED),
            (None, "BUYER_ACCEPT_GOODS", STATUS_DELIVERED),
            ("WAIT_BUYER_ACCEPT_GOODS", "SELLER_SEND_GOODS", STATUS_SHIPPED),
            ("SELLER_SEND_PART_GOODS", "SELLER_SEND_GOODS", STATUS_SHIPPED),
            (None, "SELLER_SEND_GOODS", STATUS_SHIPPED),
            ("FINISH", "BUYER_ACCEPT_GOODS", STATUS_DELIVERED),
            ("FUND_PROCESSING", "BUYER_ACCEPT_GOODS", STATUS_DELIVERED),
        ],
    )
    def test_mapeamentos_conclusivos(self, order, logi, expected):
        assert map_aliexpress_ds_status(order, logi) == expected

    @pytest.mark.parametrize(
        "order, logi",
        [
            ("FINISH", None),
            ("FINISH", "NO_LOGISTICS"),
            ("IN_CANCEL", None),
            ("IN_CANCEL", "BUYER_ACCEPT_GOODS"),
            ("IN_ISSUE", None),
            ("IN_FROZEN", None),
            ("WAIT_SELLER_EXAMINE_MONEY", None),
            ("UNKNOWN_STATUS_207", None),
            ("FUND_PROCESSING", None),
            ("FUND_PROCESSING", "NO_LOGISTICS"),
            (None, None),
            ("", ""),
        ],
    )
    def test_mapeamentos_conservadores_nao_inventam(self, order, logi):
        outcome = map_aliexpress_ds_status(order, logi)
        assert outcome == STATUS_UNKNOWN

    def test_nunca_produz_cancelled(self):
        for order, logi in [
            ("IN_CANCEL", None),
            ("FINISH", None),
            ("IN_ISSUE", None),
        ]:
            assert map_aliexpress_ds_status(order, logi) != "cancelled"


@pytest.fixture()
def ds_supplier(db):
    supplier = Supplier(
        id=uuid.uuid4(),
        name="Supp DS 8.0.7A",
        channel_type="aliexpress",
        seller_id="SELLER_DS_807",
        default_currency="USD",
    )
    db.session.add(supplier)
    db.session.commit()
    return supplier


def _persist_credential(db, supplier, *, expires_in=3600):
    from loja.integrations.aliexpress.credential_store import persist_credential

    persist_credential(
        supplier_id=supplier.id,
        seller_id=supplier.seller_id,
        access_token="tok807",
        refresh_token="r807",
        expires_in=expires_in,
    )
    db.session.commit()


class TestAdapterContract:
    def test_usa_metodo_ds_exato_e_order_id(
        self, db, ds_supplier
    ):
        _persist_credential(db, ds_supplier)
        client = ScriptedDSClient(
            payload=_envelope("SELLER_SEND_GOODS", "SELLER_SEND_GOODS")
        )
        syncer = AliExpressDSOrderStatusSyncer(client=client)
        result = syncer.sync(_req(ds_supplier.id))
        assert client.calls == [(
            DS_ORDER_STATUS_METHOD,
            {"single_order_query": {"order_id": ALI_ORDER_ID}},
        )]
        assert DS_ORDER_STATUS_METHOD == "aliexpress.trade.ds.order.get"
        assert result.outcome == STATUS_SHIPPED
        assert result.external_status == STATUS_SHIPPED

    def test_token_via_credential_store_supplier(self, db, ds_supplier):
        _persist_credential(db, ds_supplier)
        captured = {}

        def factory(access_token):
            captured["token"] = access_token
            return ScriptedDSClient(payload=_envelope("FINISH", "BUYER_ACCEPT_GOODS"))

        syncer = AliExpressDSOrderStatusSyncer(client_factory=factory)
        result = syncer.sync(_req(ds_supplier.id))
        assert captured["token"] == "tok807"
        assert result.outcome == STATUS_DELIVERED

    def test_boundary_nunca_carrega_token(self, ds_supplier):
        request = _req(ds_supplier.id)
        for banned in ("access_token", "session", "secret", "app_secret", "sign"):
            assert not hasattr(request, banned)
        fields = {
            f.name
            for f in AliExpressOrderStatusRequest.__dataclass_fields__.values()
        }
        assert fields == {
            "aliexpress_order_id",
            "order_id",
            "supplier_id",
            "ali_order_id",
            "attempt_key",
        }

    @pytest.mark.parametrize(
        "bad",
        [None, "", "   ", "curto", "a!b!c!d!e!f!g", "x" * 100],
    )
    def test_ali_order_id_invalido_sem_http(self, db, ds_supplier, bad):
        _persist_credential(db, ds_supplier)
        client = ScriptedDSClient(payload=_envelope("SELLER_SEND_GOODS", None))
        syncer = AliExpressDSOrderStatusSyncer(client=client)
        with pytest.raises(AliExpressOrderStatusSyncError):
            syncer.sync(_req(ds_supplier.id, ali_order_id=bad))
        assert client.calls == []

    def test_supplier_ausente_sem_http(self, db, ds_supplier):
        client = ScriptedDSClient(payload=_envelope("SELLER_SEND_GOODS", None))
        syncer = AliExpressDSOrderStatusSyncer(client=client)
        with pytest.raises(AliExpressOrderStatusSyncError):
            syncer.sync(_req(None))
        assert client.calls == []

    @pytest.mark.parametrize(
        "exc, fragment",
        [
            (AliExpressAuthError("auth token=SECRET_807_AUTH", code="27"),
             "autenticação"),
            (AliExpressRateLimitError("too many requests token=SECRET_807_RL", code="555"),
             "rate limit"),
            (AliExpressTransportError("timeout url=https://target"),
             "transporte"),
            (AliExpressAPIError("business err token=SECRET_807_API", code="200"),
             "erro de API"),
        ],
    )
    def test_erros_sanitizados(self, db, ds_supplier, exc, fragment):
        _persist_credential(db, ds_supplier)
        syncer = AliExpressDSOrderStatusSyncer(client=ScriptedDSClient(exc=exc))
        with pytest.raises(AliExpressOrderStatusSyncError) as err:
            syncer.sync(_req(ds_supplier.id))
        message = str(err.value)
        assert fragment in message
        for banned in ("SECRET_807", "tok807", "https:", "/sync"):
            assert banned not in message

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            "texto",
            {},
            {"result": {}},
            {"order_status": None, "logistics_status": None},
        ],
    )
    def test_payload_malformado_excecao_sanitizada(
        self, db, ds_supplier, payload
    ):
        _persist_credential(db, ds_supplier)
        syncer = AliExpressDSOrderStatusSyncer(
            client=ScriptedDSClient(payload=payload)
        )
        with pytest.raises(AliExpressOrderStatusSyncError):
            syncer.sync(_req(ds_supplier.id))

    def test_envelope_com_result_e_request_id(self, db, ds_supplier):
        _persist_credential(db, ds_supplier)
        client = ScriptedDSClient(
            payload=_envelope(
                "FINISH", "BUYER_ACCEPT_GOODS",
                request_id="req-123", result_wrapper=True,
            )
        )
        syncer = AliExpressDSOrderStatusSyncer(client=client)
        result = syncer.sync(_req(ds_supplier.id))
        assert result.outcome == STATUS_DELIVERED
        assert syncer.last_request_id == "req-123"

    def test_logistics_nao_criam_tracking(self, db, ds_supplier):
        _persist_credential(db, ds_supplier)
        payload = _envelope(
            "WAIT_BUYER_ACCEPT_GOODS", "SELLER_SEND_GOODS",
            extra={
                "logistics_no": "LP807123",
                "logistics_service": "Cainiao",
                "logistics_info_list": [{"logistics_status": "SELLER_SEND_GOODS"}],
            },
        )
        syncer = AliExpressDSOrderStatusSyncer(
            client=ScriptedDSClient(payload=payload)
        )
        result = syncer.sync(_req(ds_supplier.id))
        assert result.outcome == STATUS_SHIPPED
        assert Shipment.query.count() == 0
        assert TrackingEvent.query.count() == 0

    def test_client_factory_default_exige_env_app(self, db, ds_supplier, monkeypatch):
        _persist_credential(db, ds_supplier)
        monkeypatch.delenv("ALIEXPRESS_APP_KEY", raising=False)
        monkeypatch.delenv("ALIEXPRESS_APP_SECRET", raising=False)
        syncer = AliExpressDSOrderStatusSyncer()
        with pytest.raises(AliExpressOrderStatusSyncError) as err:
            syncer.sync(_req(ds_supplier.id))
        assert "ALIEXPRESS_APP_KEY" in str(err.value)

    def test_credencial_nao_encontrada_sanitizada(self, db, ds_supplier):
        syncer = AliExpressDSOrderStatusSyncer(
            client_factory=lambda access_token: ScriptedDSClient()
        )
        with pytest.raises(AliExpressOrderStatusSyncError) as err:
            syncer.sync(_req(ds_supplier.id))
        assert str(ds_supplier.id) not in str(err.value)
        assert "credencial" in str(err.value)

    def test_credencial_expirada_sanitizada(self, db, ds_supplier):
        _persist_credential(db, ds_supplier, expires_in=-10)
        syncer = AliExpressDSOrderStatusSyncer(
            client_factory=lambda access_token: ScriptedDSClient()
        )
        with pytest.raises(AliExpressOrderStatusSyncError) as err:
            syncer.sync(_req(ds_supplier.id))
        assert "credencial" in str(err.value)

    def test_credencial_nao_configurada_sanitizada(self, db, ds_supplier, monkeypatch):
        monkeypatch.setenv("ALIEXPRESS_CREDENTIAL_KEY", "")
        syncer = AliExpressDSOrderStatusSyncer(
            client_factory=lambda access_token: ScriptedDSClient()
        )
        with pytest.raises(AliExpressOrderStatusSyncError) as err:
            syncer.sync(_req(ds_supplier.id))
        assert "credencial" in str(err.value)


@pytest.fixture()
def ae_product(db):
    product = Product(
        id=uuid.uuid4(),
        name="Prod DS 8.0.7A",
        description="Test",
        price=Decimal("29.90"),
        source="aliexpress",
        external_id="EXT_DS_807",
        is_draft=True,
        is_active=False,
    )
    db.session.add(product)
    db.session.commit()
    return product


@pytest.fixture()
def sp_variant_factory(db):
    counter = 0

    def _create(supplier, product):
        nonlocal counter
        counter += 1
        sku = f"SKU_DS_{uuid.uuid4().hex[:8]}"
        sp_id = str(100002000 + counter)
        sp = SupplierProduct(
            id=uuid.uuid4(),
            supplier_id=supplier.id,
            product_id=product.id,
            supplier_product_id=sp_id,
            cost=Decimal("10.00"),
            currency="USD",
            is_active=True,
        )
        db.session.add(sp)
        db.session.flush()
        variant = ProductVariant(
            id=uuid.uuid4(),
            product_id=product.id,
            supplier_product_id=sp.id,
            supplier_sku_id=sku,
            sku=None,
            color="Red",
            size="M",
            attributes=[
                {"name": "Color", "value": "Red", "sku_property_id": "14", "property_value_id": "771"},
                {"name": "Size", "value": "M", "sku_property_id": "200000124", "property_value_id": "100013888"},
            ],
            stock=10,
            price=Decimal("29.90"),
            is_active=True,
        )
        db.session.add(variant)
        db.session.commit()
        return sp, variant

    return _create


def _make_paid_order_with_item(db, make_user, product, variant):
    user = make_user()
    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        checkout_type="product",
        status="paid",
        subtotal=Decimal("29.90"),
        total=Decimal("29.90"),
        stock_reserved=False,
        customer_name="U",
        customer_email=user.email,
        customer_phone="11999999999",
        zip_code="01001000",
        street="Rua",
        number="1",
        complement="",
        neighborhood="C",
        city="SP",
        state="SP",
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        OrderItem(
            id=uuid.uuid4(),
            order_id=order.id,
            product_id=product.id,
            variant_id=variant.id,
            product_name=product.name,
            quantity=1,
            unit_price=Decimal("29.90"),
            line_total=Decimal("29.90"),
        )
    )
    # Pagamento canonico: pre-condicao explicita do fulfillment pago.
    _add_approved_payment(db, order)
    return order


def _materialize_and_submit(db, make_user, supplier, product, variant):
    _persist_credential(db, supplier)
    order = _make_paid_order_with_item(db, make_user, product, variant)
    results = ensure_aliexpress_fulfillment(order)
    assert results[0].state == "ready"
    db.session.commit()
    ali = AliExpressOrder.query.filter_by(order_id=order.id).first()
    materialize_snapshot(ali, session=db.session)
    result = submit_aliexpress_order(
        ali, submitter=OkSubmitter()
    )
    db.session.commit()
    assert result.state == STATUS_SUBMITTED
    db.session.expire_all()
    return db.session.get(AliExpressOrder, ali.id)


def _ds_adapter(payload=None, exc=None):
    client = ScriptedDSClient(payload=payload, exc=exc)
    return AliExpressDSOrderStatusSyncer(client=client), client


class TestDSAdapterInEngine:
    def test_remote_confirmed_confirma(self, db, make_user, ds_supplier,
                                       ae_product, sp_variant_factory):
        sp, variant = sp_variant_factory(ds_supplier, ae_product)
        ali = _materialize_and_submit(db, make_user, ds_supplier, ae_product, variant)
        adapter, client = _ds_adapter(payload=_envelope("WAIT_SELLER_SEND_GOODS", None))
        result = sync_aliexpress_order_status(ali, syncer=adapter)
        db.session.commit()
        assert result.state == "confirmed"
        fresh = db.session.get(AliExpressOrder, ali.id)
        assert fresh.state == "confirmed"
        assert fresh.ali_status == "confirmed"
        assert fresh.confirmed_at is not None
        assert fresh.shipped_at is None
        assert fresh.delivered_at is None

    def test_remote_shipped_expede(self, db, make_user, ds_supplier,
                                   ae_product, sp_variant_factory):
        sp, variant = sp_variant_factory(ds_supplier, ae_product)
        ali = _materialize_and_submit(db, make_user, ds_supplier, ae_product, variant)
        adapter, _ = _ds_adapter(payload=_envelope("SELLER_SEND_GOODS", "SELLER_SEND_GOODS"))
        result = sync_aliexpress_order_status(ali, syncer=adapter)
        db.session.commit()
        assert result.state == "shipped"
        fresh = db.session.get(AliExpressOrder, ali.id)
        assert fresh.state == "shipped"
        assert fresh.confirmed_at is not None
        assert fresh.shipped_at is not None
        assert fresh.delivered_at is None

    def test_remote_delivered_entrega(self, db, make_user, ds_supplier,
                                      ae_product, sp_variant_factory):
        sp, variant = sp_variant_factory(ds_supplier, ae_product)
        ali = _materialize_and_submit(db, make_user, ds_supplier, ae_product, variant)
        adapter, _ = _ds_adapter(payload=_envelope(None, "BUYER_ACCEPT_GOODS"))
        result = sync_aliexpress_order_status(ali, syncer=adapter)
        db.session.commit()
        assert result.state == "delivered"
        fresh = db.session.get(AliExpressOrder, ali.id)
        assert fresh.state == "delivered"
        assert fresh.confirmed_at is not None
        assert fresh.shipped_at is not None
        assert fresh.delivered_at is not None

    def test_remote_unknown_mantem_estado(self, db, make_user, ds_supplier,
                                          ae_product, sp_variant_factory):
        sp, variant = sp_variant_factory(ds_supplier, ae_product)
        ali = _materialize_and_submit(db, make_user, ds_supplier, ae_product, variant)
        adapter, _ = _ds_adapter(payload=_envelope("IN_CANCEL", None))
        result = sync_aliexpress_order_status(ali, syncer=adapter)
        db.session.commit()
        assert result.state == "submitted"
        fresh = db.session.get(AliExpressOrder, ali.id)
        assert fresh.state == "submitted"
        assert fresh.last_error == ERROR_CODES["STATUS_SYNC_UNKNOWN"]

    def test_remote_regressivo_nao_regride(self, db, make_user, ds_supplier,
                                           ae_product, sp_variant_factory):
        sp, variant = sp_variant_factory(ds_supplier, ae_product)
        ali = _materialize_and_submit(db, make_user, ds_supplier, ae_product, variant)
        adapter, _ = _ds_adapter(payload=_envelope("SELLER_SEND_GOODS", None))
        result = sync_aliexpress_order_status(ali, syncer=adapter)
        db.session.commit()
        assert result.state == "shipped"
        shipped_at = db.session.get(AliExpressOrder, ali.id).shipped_at

        adapter_regressivo, _ = _ds_adapter(
            payload=_envelope("PLACE_ORDER_SUCCESS", None)
        )
        result2 = sync_aliexpress_order_status(ali, syncer=adapter_regressivo)
        db.session.commit()
        fresh = db.session.get(AliExpressOrder, ali.id)
        assert result2.state == "shipped"
        assert fresh.state == "shipped"
        assert fresh.shipped_at == shipped_at
        assert fresh.last_error is None
        assert fresh.ali_status == "shipped"

    def test_idempotente_repara_sem_sobrescrever(self, db, make_user, ds_supplier,
                                                 ae_product, sp_variant_factory):
        sp, variant = sp_variant_factory(ds_supplier, ae_product)
        ali = _materialize_and_submit(db, make_user, ds_supplier, ae_product, variant)
        adapter, _ = _ds_adapter(payload=_envelope("WAIT_SELLER_SEND_GOODS", None))
        sync_aliexpress_order_status(ali, syncer=adapter)
        db.session.commit()
        fresh = db.session.get(AliExpressOrder, ali.id)
        confirmed_at = fresh.confirmed_at
        assert confirmed_at is not None
        assert fresh.delivered_at is None

        adapter2, _ = _ds_adapter(payload=_envelope("WAIT_SELLER_SEND_GOODS", None))
        result2 = sync_aliexpress_order_status(ali, syncer=adapter2)
        db.session.commit()
        fresh2 = db.session.get(AliExpressOrder, ali.id)
        assert result2.state == "confirmed"
        assert fresh2.confirmed_at == confirmed_at
        assert fresh2.shipped_at is None
        assert fresh2.delivered_at is None

    def test_erro_adapter_vira_status_sync_failed(self, db, make_user, ds_supplier,
                                                  ae_product, sp_variant_factory):
        sp, variant = sp_variant_factory(ds_supplier, ae_product)
        ali = _materialize_and_submit(db, make_user, ds_supplier, ae_product, variant)
        adapter, _ = _ds_adapter(
            exc=AliExpressAuthError("auth token=SECRET_807_ERR", code="27")
        )
        result = sync_aliexpress_order_status(ali, syncer=adapter)
        db.session.commit()
        fresh = db.session.get(AliExpressOrder, ali.id)
        assert result.state == "submitted"
        assert fresh.state == "submitted"
        assert fresh.last_error == ERROR_CODES["STATUS_SYNC_FAILED"]
        assert "SECRET_807_ERR" not in (fresh.last_error or "")