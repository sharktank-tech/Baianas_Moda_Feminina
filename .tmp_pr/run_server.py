"""Servidor local de captura — dados 100% sintéticos em SQLite descartável.

Uso:  python .tmp_pr/run_server.py            (banco novo a cada execução)
      SCREEN_DB=outro.db python .tmp_pr/run_server.py
Nunca toca no .env de produção: DATABASE_URL/SECRET_KEY são fixados ANTES de
importar config (load_dotenv não sobrescreve variáveis já existentes).
"""
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

DB_PATH = os.path.join(HERE, os.environ.get("SCREEN_DB", "screens.db"))
os.environ["DATABASE_URL"] = "sqlite:///" + DB_PATH
os.environ["SECRET_KEY"] = "captura-sintetica-local"
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

# DDL dos tipos PostgreSQL em SQLite (mesma receita de tests/conftest.py).
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB, UUID  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid_sqlite(type_, compiler, **kw):
    return "VARCHAR(36)"


NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def seed(app):
    from loja import db
    from loja.modules.getway_mercado_pago.payment_models import Order, OrderItem, Payment
    from loja.modules.models import (
        ManualSupplierPurchase, Product, ProductVariant, Shipment, User,
    )
    from loja.services import manual_dispatch as dispatch

    with app.app_context():
        db.create_all()

        admin = User(
            id=uuid.uuid4(), name="Ana Operadora (sintético)",
            email="ana.admin@exemplo.test", is_admin=True,
        )
        admin.set_password("senha123")
        cliente = User(
            id=uuid.uuid4(), name="Bruno Cliente (sintético)",
            email="bruno.cliente@exemplo.test",
        )
        cliente.set_password("senha123")
        db.session.add_all([admin, cliente])
        db.session.flush()

        produto = Product(
            id=uuid.uuid4(), name="Vestido Floral Midi (sintético)",
            description="Peça de demonstração para telas administrativas.",
            price=Decimal("89.90"), image1="https://exemplo.test/vestido.png",
            category="feminino", stock=10, is_active=True,
        )
        db.session.add(produto)
        db.session.flush()
        variante = ProductVariant(
            id=uuid.uuid4(), product_id=produto.id, sku="VD-SINT-M",
            color="Azul-marinho", size="M", stock=10,
            price=Decimal("89.90"), is_active=True,
            attributes=[{"pid": 14, "vid": 193}, {"pid": 200000124, "vid": 200000335}],
        )
        db.session.add(variante)
        db.session.flush()

        def novo_pedido(numero, quantidade):
            pedido = Order(
                id=uuid.uuid4(), order_number=numero,
                user_id=cliente.id, checkout_type="product",
                status="paid", subtotal=Decimal("89.90"),
                shipping=Decimal("0.00"), discount=Decimal("0.00"),
                total=Decimal("89.90"), customer_name="Bruno Cliente (sintético)",
                customer_email="bruno.cliente@exemplo.test",
                customer_phone="61999990001",
                street="Rua Sintética das Flores", number="123",
                complement="Bloco B, Apto 21", neighborhood="Asa Norte",
                city="Brasília", state="DF", zip_code="70040010",
                mp_payment_id=uuid.uuid4().hex, mp_status="approved",
                mp_status_detail="accredited", paid_at=NOW,
                created_at=NOW, updated_at=NOW,
            )
            db.session.add(pedido)
            db.session.flush()
            pedido.payments.append(Payment(
                id=uuid.uuid4(), provider="mercado_pago",
                provider_payment_id=pedido.mp_payment_id, status="approved",
                amount=pedido.total, currency="BRL", raw_payload={},
            ))
            item = OrderItem(
                id=uuid.uuid4(), order_id=pedido.id, product_id=produto.id,
                product_name=produto.name, selected_size="M",
                variant_id=variante.id, quantity=quantidade,
                unit_price=produto.price,
                line_total=produto.price * quantidade,
                supplier_unit_cost_snapshot=Decimal("34.00"),
                supplier_cost_currency_snapshot="BRL",
            )
            db.session.add(item)
            db.session.commit()
            return pedido, item

        # A — nenhuma compra registrada (estado vazio + formulário).
        pedido_a, item_a = novo_pedido("PM-251231-A11B22C3", 1)
        # B — compra registrada, fornecedor AINDA não confirmou envio.
        pedido_b, item_b = novo_pedido("PM-251231-B22C33D4", 1)
        # C — compra + remessa postada parcial + correções auditadas.
        pedido_c, item_c = novo_pedido("PM-251231-C33D44E5", 2)

        nota_substituto = (
            "Anúncio substituto 1005000000000042; SKU 1200000000000042; "
            "Preto 38–39; custo efetivo R$ 24,47 (produto R$ 20,27 + frete "
            "R$ 0,00 + ICMS R$ 4,20); motivo: anúncio original indisponível, "
            "equivalência confirmada pelo lojista; cotação de 19/10/2026."
        )
        res_b = dispatch.register_manual_purchase(
            order_id=pedido_b.id, external_order_id="9901234567890042",
            items={item_b.id: 1}, purchased_at="2026-10-18T15:30:00",
            purchased_tz="America/Sao_Paulo",
            evidence_ref="print-pedido-9901234567890042.png",
            note=nota_substituto, admin_user_id=admin.id,
        )
        db.session.commit()

        # C: número externo cadastrado com "erro de digitação" e corrigido.
        res_c = dispatch.register_manual_purchase(
            order_id=pedido_c.id, external_order_id="990123456789000I",
            items={item_c.id: 2}, purchased_at="2026-10-17T09:15:00",
            purchased_tz="America/Sao_Paulo", evidence_ref="EV-SINT-C01",
            note="Compra de 2 unidades no anúncio sintético.",
            admin_user_id=admin.id,
        )
        db.session.commit()
        compra_c = db.session.get(ManualSupplierPurchase, res_c.purchase_id)
        dispatch.correct_manual_purchase(
            purchase_id=compra_c.id, external_order_id="9901234567890001",
            reason="Caractere digitado por engano na hora do registro.",
            admin_user_id=admin.id,
            expected_updated_at=compra_c.updated_at.isoformat(),
        )
        db.session.commit()

        # Remessa parcial: 1 de 2 postada — cobertura parcial + badge postado.
        remessa = dispatch.register_manual_shipment(
            order_id=pedido_c.id, purchase_id=res_c.purchase_id,
            tracking_code="BR7700000001SY", carrier="Correios",
            posted_at="2026-10-18T16:00:00",
            estimated_delivery="2026-10-25",
            evidence_ref="EV-SINT-C02", items={item_c.id: 1},
            admin_user_id=admin.id,
        )
        db.session.commit()
        remessa_row = db.session.get(Shipment, remessa.shipment_id)
        dispatch.correct_manual_shipment(
            shipment_id=remessa_row.id, carrier="Jadlog",
            reason="Etiqueta trocada antes da postagem.",
            admin_user_id=admin.id,
            expected_updated_at=remessa_row.updated_at.isoformat(),
        )
        db.session.commit()

        ids = {"A": str(pedido_a.id), "B": str(pedido_b.id), "C": str(pedido_c.id)}
        with open(os.path.join(HERE, "seed_ids.json"), "w") as fh:
            json.dump(ids, fh, indent=2)
        print("SEED_OK", json.dumps(ids))


def main():
    fresh = os.environ.get("SCREEN_FRESH", "1") == "1"
    if fresh and os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    from loja import create_app
    app = create_app()
    if os.environ.get("SEED_SKIP") != "1":
        seed(app)
    port = int(os.environ.get("PORTA", "5131"))
    print(f"SERVIDOR_EM http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
