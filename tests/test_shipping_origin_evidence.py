"""Origem de envio por variante com evidência (integração AliExpress, Etapa 1).

Fail-closed: sem linha = desconhecida (nunca nacional); evidência manual
obrigatória; observação da API nunca sobrescreve a confirmação manual;
seleção de operação nacional sem prova bloqueia readiness e publicação.
Sem chamada de frete, sem provider, sem compra, sem pedido.
"""

import uuid
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

FROZEN_NOW = datetime(2030, 6, 15, 10, 0)


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    monkeypatch.setattr("loja.utils.utcnow", lambda: FROZEN_NOW)
    return FROZEN_NOW


@pytest.fixture(autouse=True)
def _cleanup_session(db):
    yield
    db.session.rollback()
    db.session.remove()


def _product(db, **overrides):
    from loja.modules.models import Product

    params = {
        "id": uuid.uuid4(),
        "name": "Produto Origem",
        "description": "Descrição do produto de origem",
        "price": Decimal("49.90"),
        "image1": "https://exemplo.com/img.png",
        "stock": 10,
        "is_active": True,
        "is_draft": False,
        "source": "aliexpress",
        "external_id": "900" + uuid.uuid4().hex[:12],
        "currency": "BRL",
        "target_margin_percent": Decimal("30.00"),
        "pricing_status": "priced",
    }
    params.update(overrides)
    product = Product(**params)
    db.session.add(product)
    db.session.flush()
    return product


def _supplier(db, **overrides):
    from loja.modules.models import Supplier

    params = {
        "id": uuid.uuid4(),
        "name": "AliExpress",
        "channel_type": "aliexpress",
        "seller_id": "SELLER-ORIGIN-1",
    }
    params.update(overrides)
    supplier = Supplier(**params)
    db.session.add(supplier)
    db.session.flush()
    return supplier


def _link(db, product, supplier, **overrides):
    from loja.modules.models import SupplierProduct

    params = {
        "id": uuid.uuid4(),
        "supplier_id": supplier.id,
        "product_id": product.id,
        "supplier_product_id": "SP-ORIGIN-" + uuid.uuid4().hex[:8],
        "cost": Decimal("20.00"),
        "currency": "BRL",
        "freight_status": "available",
        "shipping_cost": Decimal("5.00"),
        "import_duty": Decimal("0.00"),
        "icms": Decimal("0.00"),
        "landed_cost": Decimal("25.00"),
        "freight_currency": "BRL",
        "is_active": True,
    }
    params.update(overrides)
    link = SupplierProduct(**params)
    db.session.add(link)
    db.session.flush()
    return link


def _variant(db, product, link=None, *, sku=None, selected=False, **overrides):
    from loja.modules.models import ProductVariant

    params = {
        "id": uuid.uuid4(),
        "product_id": product.id,
        "color": "Rosa",
        "size": "M",
        "stock": 3,
        "is_active": True,
        "supplier_product_id": link.id if link is not None else None,
        "supplier_sku_id": sku,
        "national_shipping_operation": selected,
    }
    params.update(overrides)
    variant = ProductVariant(**params)
    db.session.add(variant)
    db.session.flush()
    return variant


def _confirm(db, link, origin, *, sku_id=None, covers_all=False, ref="PROVA-1",
             country="BR", user_id=None):
    from loja.services.shipping_origin import record_manual_confirmation

    row = record_manual_confirmation(
        supplier_product_id=link.id,
        origin=origin,
        evidence_ref=ref,
        sku_id=sku_id,
        covers_all=covers_all,
        origin_country=country,
        user_id=user_id,
        now=FROZEN_NOW,
    )
    db.session.commit()
    return row


def _observe(db, link, origin, *, sku_id=None, covers_all=False, country="CN"):
    from loja.services.shipping_origin import record_api_observation

    row = record_api_observation(
        supplier_product_id=link.id,
        origin=origin,
        sku_id=sku_id,
        covers_all=covers_all,
        origin_country=country,
        now=FROZEN_NOW,
    )
    db.session.commit()
    return row


# ── resolução: desconhecido nunca é nacional ──────────────────────────────

def test_no_row_never_resolves_as_national(db):
    from loja.services.shipping_origin import (
        LABEL_UNKNOWN,
        resolve_variant_origin,
        resolve_product_summary,
    )

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    variant = _variant(db, product, link, sku="SKU-1")

    resolution = resolve_variant_origin(variant)
    assert resolution.origin is None
    assert resolution.evidenced is False
    assert resolution.known is False
    assert resolve_product_summary(product).label == LABEL_UNKNOWN


def test_variant_without_supplier_link_is_unknown(db):
    from loja.services.shipping_origin import resolve_variant_origin

    product = _product(db)
    variant = _variant(db, product, None)

    assert resolve_variant_origin(variant).origin is None


# ── validação da confirmação manual ───────────────────────────────────────

def test_manual_confirmation_requires_evidence_ref(db):
    from loja.services.shipping_origin import (
        ShippingOriginError,
        record_manual_confirmation,
    )

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)

    with pytest.raises(ShippingOriginError) as excinfo:
        record_manual_confirmation(
            supplier_product_id=link.id,
            origin="brazil",
            evidence_ref="   ",
            covers_all=True,
        )
    assert excinfo.value.args[0] == "evidence_ref_required"
    db.session.rollback()


def test_manual_confirmation_rejects_long_evidence_ref(db):
    from loja.services.shipping_origin import (
        ShippingOriginError,
        record_manual_confirmation,
    )

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)

    with pytest.raises(ShippingOriginError) as excinfo:
        record_manual_confirmation(
            supplier_product_id=link.id,
            origin="brazil",
            evidence_ref="x" * 201,
            covers_all=True,
        )
    assert excinfo.value.args[0] == "evidence_ref_too_long"
    db.session.rollback()


def test_invalid_origin_and_country_rejected(db):
    from loja.services.shipping_origin import (
        ShippingOriginError,
        record_manual_confirmation,
    )

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)

    for origin, country, code in (
        ("nacional", "BR", "origin_invalid"),
        ("brazil", "BRA", "origin_country_invalid"),
        ("", "BR", "origin_required"),
    ):
        with pytest.raises(ShippingOriginError) as excinfo:
            record_manual_confirmation(
                supplier_product_id=link.id,
                origin=origin,
                evidence_ref="PROVA-1",
                covers_all=True,
                origin_country=country,
            )
        assert excinfo.value.args[0] == code
        db.session.rollback()


def test_scope_is_xor(db):
    from loja.services.shipping_origin import (
        ShippingOriginError,
        clear_manual_confirmation,
        record_manual_confirmation,
    )

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)

    with pytest.raises(ShippingOriginError) as excinfo:
        record_manual_confirmation(
            supplier_product_id=link.id, origin="brazil",
            evidence_ref="PROVA-1",
        )
    assert excinfo.value.args[0] == "scope_required"
    db.session.rollback()

    with pytest.raises(ShippingOriginError) as excinfo:
        record_manual_confirmation(
            supplier_product_id=link.id, origin="brazil",
            evidence_ref="PROVA-1", sku_id="SKU-1", covers_all=True,
        )
    assert excinfo.value.args[0] == "scope_invalid"
    db.session.rollback()

    with pytest.raises(ShippingOriginError):
        clear_manual_confirmation(supplier_product_id=link.id)
    db.session.rollback()


def test_manual_confirmation_is_idempotent_per_scope(db):
    from loja.modules.models import SupplierSkuShippingOrigin
    from loja.services.shipping_origin import resolve_variant_origin

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    variant = _variant(db, product, link, sku="SKU-1")

    _confirm(db, link, "brazil", sku_id="SKU-1", ref="PROVA-1")
    _confirm(db, link, "brazil", sku_id="SKU-1", ref="PROVA-2")

    rows = SupplierSkuShippingOrigin.query.filter_by(
        supplier_product_id=link.id
    ).all()
    assert len(rows) == 1
    assert rows[0].evidence_ref == "PROVA-2"
    assert resolve_variant_origin(variant).origin == "brazil"


# ── precedência SKU > cobertura e conflito manual × api ──────────────────

def test_sku_row_takes_precedence_over_cover_all(db):
    from loja.services.shipping_origin import (
        SCOPE_SKU,
        resolve_variant_origin,
    )

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    variant = _variant(db, product, link, sku="SKU-1")

    _confirm(db, link, "foreign", covers_all=True, ref="PROVA-COVER")
    _confirm(db, link, "brazil", sku_id="SKU-1", ref="PROVA-SKU")

    resolution = resolve_variant_origin(variant)
    assert resolution.origin == "brazil"
    assert resolution.scope == SCOPE_SKU


def test_cover_all_resolves_unlisted_sku(db):
    from loja.services.shipping_origin import SCOPE_COVER_ALL

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    variant = _variant(db, product, link, sku="SKU-OUTRA")

    _confirm(db, link, "foreign", covers_all=True, ref="PROVA-COVER")

    from loja.services.shipping_origin import resolve_variant_origin

    resolution = resolve_variant_origin(variant)
    assert resolution.origin == "foreign"
    assert resolution.scope == SCOPE_COVER_ALL


def test_api_observation_never_overwrites_manual_confirmation(db):
    from loja.services.shipping_origin import resolve_variant_origin

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    variant = _variant(db, product, link, sku="SKU-1")

    _confirm(db, link, "brazil", sku_id="SKU-1", ref="PROVA-SKU")
    _observe(db, link, "foreign", sku_id="SKU-1")

    resolution = resolve_variant_origin(variant)
    assert resolution.row.manual_origin == "brazil"
    assert resolution.row.api_origin == "foreign"
    assert resolution.origin == "brazil"
    assert resolution.conflict is True


def test_manual_confirmation_preserves_api_columns(db):
    from loja.services.shipping_origin import resolve_variant_origin

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    variant = _variant(db, product, link, sku="SKU-1")

    _observe(db, link, "foreign", sku_id="SKU-1")
    _confirm(db, link, "brazil", sku_id="SKU-1", ref="PROVA-SKU")

    resolution = resolve_variant_origin(variant)
    assert resolution.row.api_origin == "foreign"
    assert resolution.row.api_observed_at == FROZEN_NOW
    assert resolution.origin == "brazil"
    assert resolution.conflict is True


def test_confluent_manual_and_api_has_no_conflict(db):
    from loja.services.shipping_origin import resolve_variant_origin

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    variant = _variant(db, product, link, sku="SKU-1")

    _observe(db, link, "brazil", sku_id="SKU-1")
    _confirm(db, link, "brazil", sku_id="SKU-1", ref="PROVA-SKU")

    resolution = resolve_variant_origin(variant)
    assert resolution.conflict is False
    assert resolution.origin == "brazil"


def test_api_observation_alone_is_evidence(db):
    from loja.services.shipping_origin import resolve_variant_origin

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    variant = _variant(db, product, link, sku="SKU-1")

    _observe(db, link, "foreign", sku_id="SKU-1")

    resolution = resolve_variant_origin(variant)
    assert resolution.origin == "foreign"
    assert resolution.evidenced is True
    assert resolution.conflict is False


# ── limpeza da confirmação manual ─────────────────────────────────────────

def test_clear_manual_keeps_api_observation(db):
    from loja.services.shipping_origin import (
        clear_manual_confirmation,
        resolve_variant_origin,
    )

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    variant = _variant(db, product, link, sku="SKU-1")

    _confirm(db, link, "brazil", sku_id="SKU-1", ref="PROVA-SKU")
    _observe(db, link, "foreign", sku_id="SKU-1")

    assert clear_manual_confirmation(
        supplier_product_id=link.id, sku_id="SKU-1"
    ) is True
    db.session.commit()

    resolution = resolve_variant_origin(variant)
    assert resolution.row.manual_origin is None
    assert resolution.origin == "foreign"
    assert resolution.conflict is False


def test_clear_manual_deletes_empty_row(db):
    from loja.modules.models import SupplierSkuShippingOrigin
    from loja.services.shipping_origin import clear_manual_confirmation

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)

    _confirm(db, link, "brazil", covers_all=True, ref="PROVA-COVER")
    assert clear_manual_confirmation(
        supplier_product_id=link.id, covers_all=True
    ) is True
    db.session.commit()

    assert (
        SupplierSkuShippingOrigin.query.filter_by(
            supplier_product_id=link.id
        ).count()
        == 0
    )


def test_clear_missing_row_is_false(db):
    from loja.services.shipping_origin import clear_manual_confirmation

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)

    assert clear_manual_confirmation(
        supplier_product_id=link.id, covers_all=True
    ) is False
    db.session.rollback()


# ── resumo do produto: 1 SKU BR nunca vira nacional ───────────────────────

def test_summary_single_brazil_variant_never_national(db):
    from loja.services.shipping_origin import LABEL_MIXED, resolve_product_summary

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    _variant(db, product, link, sku="SKU-1")
    _variant(db, product, link, sku="SKU-2")

    _confirm(db, link, "brazil", sku_id="SKU-1", ref="PROVA-1")

    summary = resolve_product_summary(product)
    assert summary.label == LABEL_MIXED
    assert summary.brazil == 1
    assert summary.unknown == 1
    assert summary.is_national is False


def test_summary_all_brazil_is_national(db):
    from loja.services.shipping_origin import LABEL_BRAZIL, resolve_product_summary

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    _variant(db, product, link, sku="SKU-1")
    _variant(db, product, link, sku="SKU-2")

    _confirm(db, link, "brazil", covers_all=True, ref="PROVA-COVER")

    summary = resolve_product_summary(product)
    assert summary.label == LABEL_BRAZIL
    assert summary.total == 2
    assert summary.unknown == 0
    assert summary.is_national is True


def test_summary_all_unknown(db):
    from loja.services.shipping_origin import LABEL_UNKNOWN, resolve_product_summary

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    _variant(db, product, link, sku="SKU-1")

    assert resolve_product_summary(product).label == LABEL_UNKNOWN


def test_summary_mixed_brazil_and_foreign(db):
    from loja.services.shipping_origin import LABEL_MIXED, resolve_product_summary

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    _variant(db, product, link, sku="SKU-1")
    _variant(db, product, link, sku="SKU-2")

    _confirm(db, link, "brazil", sku_id="SKU-1", ref="PROVA-1")
    _confirm(db, link, "foreign", sku_id="SKU-2", ref="PROVA-2")

    summary = resolve_product_summary(product)
    assert summary.label == LABEL_MIXED
    assert summary.brazil == 1
    assert summary.foreign == 1


def test_summary_foreign_when_all_foreign(db):
    from loja.services.shipping_origin import LABEL_FOREIGN, resolve_product_summary

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    _variant(db, product, link, sku="SKU-1")
    _variant(db, product, link, sku="SKU-2")

    _confirm(db, link, "foreign", covers_all=True, ref="PROVA-COVER")

    assert resolve_product_summary(product).label == LABEL_FOREIGN


def test_summary_conflict_label(db):
    from loja.services.shipping_origin import LABEL_CONFLICT, resolve_product_summary

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    _variant(db, product, link, sku="SKU-1")

    _confirm(db, link, "brazil", sku_id="SKU-1", ref="PROVA-1")
    _observe(db, link, "foreign", sku_id="SKU-1")

    assert resolve_product_summary(product).label == LABEL_CONFLICT


def test_summary_without_variants_uses_cover_all(db):
    from loja.services.shipping_origin import LABEL_BRAZIL, resolve_product_summary

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)

    _confirm(db, link, "brazil", covers_all=True, ref="PROVA-COVER")

    assert resolve_product_summary(product).label == LABEL_BRAZIL


def test_inactive_variant_does_not_define_summary(db):
    from loja.services.shipping_origin import LABEL_UNKNOWN, resolve_product_summary

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    # A variante VENDÁVEL está sem prova; a inativa confirmada BR não muda
    # o que o cliente compraria hoje.
    _variant(db, product, link, sku="SKU-ATIVA")
    _variant(db, product, link, sku="SKU-INATIVA", is_active=False)

    _confirm(db, link, "brazil", sku_id="SKU-INATIVA", ref="PROVA-1")

    assert resolve_product_summary(product).label == LABEL_UNKNOWN


# ── gate de operação nacional ─────────────────────────────────────────────

def test_gate_without_selection_returns_no_reasons(db):
    from loja.services.shipping_origin import national_operation_reasons

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    _variant(db, product, link, sku="SKU-1")

    assert national_operation_reasons(product) == []


def test_gate_selected_without_evidence_blocks(db):
    from loja.services.shipping_origin import (
        REASON_NATIONAL_UNPROVEN,
        national_operation_reasons,
    )

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    _variant(db, product, link, sku="SKU-1", selected=True)

    assert national_operation_reasons(product) == [REASON_NATIONAL_UNPROVEN]


def test_gate_selected_with_brazil_evidence_clears(db):
    from loja.services.shipping_origin import national_operation_reasons

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    _variant(db, product, link, sku="SKU-1", selected=True)

    _confirm(db, link, "brazil", sku_id="SKU-1", ref="PROVA-1")

    assert national_operation_reasons(product) == []


def test_gate_selected_foreign_blocks(db):
    from loja.services.shipping_origin import (
        REASON_NATIONAL_FOREIGN,
        national_operation_reasons,
    )

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    _variant(db, product, link, sku="SKU-1", selected=True)

    _confirm(db, link, "foreign", sku_id="SKU-1", ref="PROVA-1")

    assert national_operation_reasons(product) == [REASON_NATIONAL_FOREIGN]


def test_gate_conflict_blocks(db):
    from loja.services.shipping_origin import (
        REASON_NATIONAL_CONFLICT,
        national_operation_reasons,
    )

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    _variant(db, product, link, sku="SKU-1", selected=True)

    _confirm(db, link, "brazil", sku_id="SKU-1", ref="PROVA-1")
    _observe(db, link, "foreign", sku_id="SKU-1")

    assert national_operation_reasons(product) == [REASON_NATIONAL_CONFLICT]


def test_gate_ignores_inactive_and_unselected(db):
    from loja.services.shipping_origin import national_operation_reasons

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    # selecionada mas INATIVA: fora do gate.
    _variant(db, product, link, sku="SKU-1", selected=True, is_active=False)
    # ativa mas NÃO selecionada: fora do gate (catálogo internacional).
    _variant(db, product, link, sku="SKU-2", selected=False)

    assert national_operation_reasons(product) == []


def test_gate_reasons_are_ordered_and_distinct(db):
    from loja.services.shipping_origin import (
        NATIONAL_REASON_ORDER,
        national_operation_reasons,
    )

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    _variant(db, product, link, sku="SKU-1", selected=True)
    _variant(db, product, link, sku="SKU-2", selected=True)

    # SKU-1 sem evidência; SKU-2 conflitante.
    _confirm(db, link, "brazil", sku_id="SKU-2", ref="PROVA-2")
    _observe(db, link, "foreign", sku_id="SKU-2")

    reasons = national_operation_reasons(product)
    assert reasons == [
        code for code in NATIONAL_REASON_ORDER if code in set(reasons)
    ]
    assert len(reasons) == len(set(reasons))


# ── seleção explícita ─────────────────────────────────────────────────────

def test_set_national_operation_selects_only_active_variants(db):
    from loja.services.shipping_origin import set_national_operation

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    active = _variant(db, product, link, sku="SKU-1")
    inactive = _variant(db, product, link, sku="SKU-2", is_active=False)

    selected = set_national_operation(product, [str(active.id), str(inactive.id)])
    db.session.commit()

    assert selected == 1
    assert active.national_shipping_operation is True
    assert inactive.national_shipping_operation is False


def test_set_national_operation_none_clears_all(db):
    from loja.services.shipping_origin import set_national_operation

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    variant = _variant(db, product, link, sku="SKU-1", selected=True)

    assert set_national_operation(product, None) == 0
    db.session.commit()
    assert variant.national_shipping_operation is False


def test_set_national_operation_ignores_foreign_ids(db):
    from loja.services.shipping_origin import set_national_operation

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    other_product = _product(db)
    other_variant = _variant(db, other_product, None)
    variant = _variant(db, product, link, sku="SKU-1")

    selected = set_national_operation(product, [str(other_variant.id)])
    db.session.commit()

    assert selected == 0
    assert variant.national_shipping_operation is False


def test_national_flag_defaults_to_false(db):
    from loja.modules.models import ProductVariant

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    variant = _variant(db, product, link, sku="SKU-1")

    fresh = db.session.get(ProductVariant, variant.id)
    assert fresh.national_shipping_operation is False


# ── defesas do banco (CHECKs e índice único parcial) ──────────────────────

def test_db_rejects_manual_origin_without_evidence(db):
    from loja.modules.models import SupplierSkuShippingOrigin

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)

    db.session.add(
        SupplierSkuShippingOrigin(
            supplier_product_id=link.id,
            supplier_sku_id=None,
            covers_all_variants=True,
            manual_origin="brazil",
        )
    )
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()


def test_db_rejects_invalid_origin_value(db):
    from loja.modules.models import SupplierSkuShippingOrigin

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)

    db.session.add(
        SupplierSkuShippingOrigin(
            supplier_product_id=link.id,
            supplier_sku_id=None,
            covers_all_variants=True,
            api_origin="us",
            api_observed_at=FROZEN_NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()


def test_db_rejects_scope_violation(db):
    from loja.modules.models import SupplierSkuShippingOrigin

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)

    db.session.add(
        SupplierSkuShippingOrigin(
            supplier_product_id=link.id,
            supplier_sku_id="SKU-1",
            covers_all_variants=True,
            api_origin="foreign",
            api_observed_at=FROZEN_NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()


def test_db_one_row_per_sku_and_per_cover_all(db):
    from loja.modules.models import SupplierSkuShippingOrigin

    product = _product(db)
    supplier = _supplier(db)
    link = _link(db, product, supplier)
    _confirm(db, link, "brazil", sku_id="SKU-1", ref="PROVA-1")
    _confirm(db, link, "brazil", covers_all=True, ref="PROVA-COVER")

    db.session.add(
        SupplierSkuShippingOrigin(
            supplier_product_id=link.id,
            supplier_sku_id="SKU-1",
            covers_all_variants=False,
            api_origin="foreign",
            api_observed_at=FROZEN_NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()
