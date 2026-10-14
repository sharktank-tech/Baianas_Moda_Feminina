"""Semeia produto com muitas imagens (12, orientações mistas) no banco
descartável do CI.

Roda em subprocesso próprio (sem o conftest do pytest, que troca
DATABASE_URL por SQLite): usa BROWSER_DATABASE_URL do workflow.
Imprime o UUID do produto. Imagens picsum.photos com dimensões que
misturam vertical/horizontal/quadrado.
"""
import os
import sys
import uuid
from decimal import Decimal

os.environ["DATABASE_URL"] = (
    os.environ.get("BROWSER_DATABASE_URL")
    or os.environ.get("DATABASE_URL", "")
)

NAME = "Vestido Galeria Muitas"

DIMS = [
    (400, 500), (800, 400), (500, 500), (400, 600), (700, 350),
    (500, 500), (450, 550), (900, 450), (500, 500), (400, 500),
    (800, 400), (500, 500),
]


def main():
    sys.path.insert(0, os.getcwd())
    from run import app
    from loja import db
    from loja.modules.models import Product, ProductImage
    with app.app_context():
        prod = Product.query.filter_by(name=NAME).first()
        first_url = f"https://picsum.photos/seed/galeria0/{DIMS[0][0]}/{DIMS[0][1]}"
        if prod is None:
            prod = Product(
                id=uuid.uuid4(), name=NAME,
                description="Produto sintético p/ galeria desktop.",
                price=Decimal("129.90"),
                image1=first_url,  # igual à 1ª ProductImage: deduplica (12)
                category="feminino", stock=30, is_active=True)
            db.session.add(prod)
            db.session.flush()
        else:
            prod.image1 = first_url
        ProductImage.query.filter_by(product_id=prod.id).delete()
        for i, (w, h) in enumerate(DIMS):
            db.session.add(ProductImage(
                id=uuid.uuid4(), product_id=prod.id,
                url=f"https://picsum.photos/seed/galeria{i}/{w}/{h}",
                alt=f"Imagem {i + 1}", position=i,
                is_primary=(i == 0)))
        db.session.commit()
        print(str(prod.id))


if __name__ == "__main__":
    main()
