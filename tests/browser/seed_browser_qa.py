"""Seed sintético para os testes de navegador (CI, banco descartável).

Cria admin, cliente, produto e pedidos variados. Nenhum pagamento real,
nenhuma chamada externa, nenhum e-mail. Uso:
    BROWSER_ADMIN_EMAIL=... BROWSER_ADMIN_PASSWORD=... python seed_browser_qa.py
Com DATABASE_URL/POLLOMODAS_SCHEMA/SECRET_KEY do ambiente.
"""
import os
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.getcwd())

from run import app  # noqa: E402
from loja import db  # noqa: E402
from sqlalchemy import text as _text  # noqa: E402
from loja.modules.models import Product, User  # noqa: E402
from loja.modules.getway_mercado_pago.payment_models import (  # noqa: E402
    Order, OrderItem,
)

NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def main():
    admin_email = os.environ["BROWSER_ADMIN_EMAIL"]
    admin_password = os.environ["BROWSER_ADMIN_PASSWORD"]
    with app.app_context():
        assert db.session.execute(_text("SELECT current_schema()")).scalar() \
            not in ("public", "pollomodas"), "nunca no schema compartilhado"
        admin = User.query.filter_by(email=admin_email).first()
        if admin is None:
            admin = User(id=uuid.uuid4(), name="Browser Admin",
                         email=admin_email, phone="11999999999", is_admin=True)
            admin.set_password(admin_password)
            db.session.add(admin)
        else:
            admin.set_password(admin_password)
            admin.is_admin = True
        customer = User.query.filter_by(
            email="browser.cliente@sintetico.test").first()
        if customer is None:
            customer = User(
                id=uuid.uuid4(), name="Browser Cliente",
                email="browser.cliente@sintetico.test",
                phone="11888888888", is_admin=False)
            customer.set_password("cliente123")
            db.session.add(customer)
        product = Product.query.filter_by(name="Vestido Sintético Browser").first()
        if product is None:
            product = Product(
                id=uuid.uuid4(), name="Vestido Sintético Browser",
                description="Produto sintético para testes de navegador.",
                price=Decimal("89.90"),
                image1="https://exemplo.com/browser.png",
                category="feminino", stock=50, is_active=True)
            db.session.add(product)
        db.session.flush()
        for status, qty in (("paid", 1), ("manual_review", 1),
                            ("pending_payment", 1)):
            exists = Order.query.filter_by(
                user_id=customer.id, status=status).first()
            if exists is not None:
                continue
            order = Order(
                id=uuid.uuid4(), user_id=customer.id, checkout_type="product",
                status=status, subtotal=Decimal("89.90"),
                shipping=Decimal("10.00"), discount=Decimal("0.00"),
                total=Decimal("99.90"), customer_name="Browser Cliente",
                customer_email=customer.email, zip_code="77066520",
                street="Rua Browser", number="1", neighborhood="Centro",
                city="Palmas", state="TO")
            db.session.add(order)
            db.session.flush()
            db.session.add(OrderItem(
                id=uuid.uuid4(), order_id=order.id, product_id=product.id,
                product_name=product.name, quantity=qty,
                unit_price=product.price,
                line_total=product.price * qty))
        db.session.commit()
        print("seed browser QA OK")


def seed_newsletter():
    """Assinante + token sintetico para os testes browser de newsletter."""
    from loja.modules.models import NewsletterSubscriber
    from loja.modules.newsletter import generate_unsubscribe_token

    email = "browser.newsletter@sintetico.test"
    subscriber = NewsletterSubscriber.query.filter_by(email=email).first()
    if subscriber is None:
        subscriber = NewsletterSubscriber(id=uuid.uuid4(), email=email)
        db.session.add(subscriber)
        db.session.commit()
    token = generate_unsubscribe_token(subscriber)
    with open("/tmp/browser_newsletter_token.txt", "w", encoding="utf-8") as f:
        f.write(token)
    print(f"newsletter seed OK: {email} token={token[:16]}...")


if __name__ == "__main__":
    main()
    with app.app_context():
        seed_newsletter()
