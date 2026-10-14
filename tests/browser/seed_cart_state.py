"""Semeia o carrinho do cliente de teste no banco descartável do CI.

Roda em subprocesso próprio (sem o conftest do pytest, que troca
DATABASE_URL por SQLite): herda o ambiente do job (PostgreSQL
descartável). Uso: seed_cart_state.py '[[3],[3]]' — uma entrada [qtd]
por produto (Vestido + Blusa sintéticos).
"""
import json
import os
import sys
import uuid
from decimal import Decimal

CUSTOMER_EMAIL = "browser.cliente@sintetico.test"


def main(entries):
    sys.path.insert(0, os.getcwd())
    from run import app
    from loja import db
    from loja.modules.models import Cart, CartItem, Product, User
    with app.app_context():
        customer = User.query.filter_by(email=CUSTOMER_EMAIL).first()
        assert customer is not None, "seed do cliente ausente"
        cart = Cart.query.filter_by(user_id=customer.id).first()
        if cart is None:
            cart = Cart(id=uuid.uuid4(), user_id=customer.id)
            db.session.add(cart)
            db.session.flush()
        CartItem.query.filter_by(cart_id=cart.id).delete()
        products = []
        for i in range(max(1, len(entries))):
            name = ("Vestido Sintético Browser" if i == 0
                    else "Blusa Sintética Badge")
            prod = Product.query.filter_by(name=name).first()
            if prod is None:
                prod = Product(
                    id=uuid.uuid4(), name=name,
                    description="Produto sintético p/ badge.",
                    price=Decimal("89.90"),
                    image1="https://exemplo.com/badge.png",
                    category="feminino", stock=200, is_active=True)
                db.session.add(prod)
                db.session.flush()
            products.append(prod)
        for prod, (qty,) in zip(products, entries):
            db.session.add(CartItem(
                id=uuid.uuid4(), cart_id=cart.id, product_id=prod.id,
                selected_size="M", quantity=qty, unit_price=prod.price))
        db.session.commit()
        total = sum(q for (q,) in entries)
        print(f"carrinho semeado: {total} un.")


if __name__ == "__main__":
    main(json.loads(sys.argv[1]))
