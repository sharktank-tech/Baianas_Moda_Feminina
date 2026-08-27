"""Coerência de metadados: compatibilidade de tipo das FKs (modelos).

Executa em SQLite (não requer PostgreSQL). Valida que, para toda chave
estrangeira declarada nos models, a coluna local e a coluna referenciada têm
tipos UUID-compatíveis entre si (ou ambas não-UUID). Isso captura, em tempo de
importação, divergências como users.id BIGINT referenciado por favorites.user_id
UUID — o tipo de erro que motivou o isolamento por schema.

Não toca em nenhum banco.
"""
import pytest
from sqlalchemy import UUID

from loja import db
from loja.modules.models import (
    User, Product, Category, ProductVariant, SupplierProduct,
    CartItem, Cart, OrderItem, Supplier, Coupon, Favorite,
    Shipment, TrackingEvent, SyncLog,
)
from loja.modules.getway_mercado_pago.payment_models import (
    Order, OrderItem as PaymentOrderItem, Payment,
)


def _is_uuid(column):
    return isinstance(column.type, UUID)


def _fk_problems():
    problems = []
    for table in db.metadata.tables.values():
        for column in table.columns:
            for fk in column.foreign_keys:
                # fk.column é a Column referenciada; column é a local.
                ref = fk.column
                if ref is None:
                    problems.append(
                        (table.name, column.name,
                         "FK sem coluna referenciada (%s)" % fk.target_fullname)
                    )
                    continue
                if _is_uuid(column) != _is_uuid(ref):
                    problems.append(
                        (
                            table.name, column.name, str(column.type),
                            ref.table.name, ref.name, str(ref.type),
                            fk.target_fullname,
                        )
                    )
    return problems


def test_fk_type_compatibility():
    problems = _fk_problems()
    assert not problems, "FKs com tipo incompatível (UUID x não-UUID): %r" % problems


def test_every_fk_resolves_to_a_table():
    # toda FK aponta para tabela existente nos metadados
    missing = []
    tables = set(db.metadata.tables.keys())
    for table in db.metadata.tables.values():
        for column in table.columns:
            for fk in column.foreign_keys:
                ref = fk.column
                if ref is not None and ref.table.name not in tables:
                    missing.append((table.name, column.name,
                                    getattr(fk, "target_fullname", None)))
    assert not missing, "FK referencia tabela ausente dos metadados: %r" % missing
