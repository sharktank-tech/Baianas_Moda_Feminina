"""Catálogo fictício para demonstração da Baianas Moda Feminina.

Preview: python scripts/seed_mvp_demo.py
Gravar:  python scripts/seed_mvp_demo.py --apply

Registros identificados por external_id=baianas-demo-v1-*. Nunca apaga
ou altera produtos, categorias ou slides preexistentes.
Imagens ilustrativas de Unsplash; NÃO representam estoque ou produtos reais.
"""
import argparse
import sys
from decimal import Decimal
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run import app
from loja import db
from loja.modules.models import Category, Product, ProductVariant, ProductImage, HeroSlide

PREFIX = "baianas-demo-v1-"
CATEGORIES = [
    ("vestidos", "Vestidos"),
    ("conjuntos", "Conjuntos"),
    ("blusas", "Blusas e Croppeds"),
    ("saias", "Saias"),
    ("calcas", "Calças"),
    ("moda-casual", "Moda Casual"),
]
# Fotografias ilustrativas de moda, não representações exatas das mercadorias.
IMAGES = [
    "https://images.unsplash.com/photo-1496747611176-843222e1e57c?w=900&auto=format&fit=crop",
    "https://images.unsplash.com/photo-1539109136881-3be0616acf4b?w=900&auto=format&fit=crop",
    "https://images.unsplash.com/photo-1483985988355-763728e1935b?w=900&auto=format&fit=crop",
    "https://images.unsplash.com/photo-1529139574466-a303027c1d8b?w=900&auto=format&fit=crop",
    "https://images.unsplash.com/photo-1515372039744-b8f02a3ae446?w=900&auto=format&fit=crop",
    "https://images.unsplash.com/photo-1485968579580-b6d095142e6e?w=900&auto=format&fit=crop",
]
# slug, categoria, nome, preço, cor, unidades por tamanho, imagem index
PRODUCTS = [
    ("vestido-floral", "vestidos", "Vestido Floral Primavera [DEMO]", "139.90", "Floral", (4, 6, 3), 0),
    ("vestido-midi", "vestidos", "Vestido Midi Elegance [DEMO]", "169.90", "Vinho", (3, 4, 2), 1),
    ("conjunto-urbano", "conjuntos", "Conjunto Casual Urbano [DEMO]", "189.90", "Rosa", (5, 4, 2), 2),
    ("conjunto-leve", "conjuntos", "Conjunto Leve de Verão [DEMO]", "159.90", "Bege", (4, 4, 3), 3),
    ("blusa-romantica", "blusas", "Blusa Romântica [DEMO]", "79.90", "Branco", (8, 7, 6), 4),
    ("cropped-delicado", "blusas", "Cropped Delicado [DEMO]", "69.90", "Rosa", (7, 5, 4), 5),
    ("saia-midi", "saias", "Saia Midi Versátil [DEMO]", "109.90", "Vinho", (5, 4, 3), 0),
    ("saia-casual", "saias", "Saia Casual [DEMO]", "99.90", "Preto", (5, 5, 4), 1),
    ("calca-reta", "calcas", "Calça Reta [DEMO]", "149.90", "Jeans", (3, 5, 4), 2),
    ("calca-alfaiataria", "calcas", "Calça Alfaiataria [DEMO]", "179.90", "Bege", (3, 3, 2), 3),
    ("look-passeio", "moda-casual", "Look Passeio [DEMO]", "129.90", "Rosa", (5, 5, 5), 4),
    ("look-fim-semana", "moda-casual", "Look Fim de Semana [DEMO]", "119.90", "Azul", (5, 6, 4), 5),
]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Inserir dados no banco (padrao: visualizar)")
    args = parser.parse_args()
    with app.app_context():
        schema = app.config.get("BAIANAS_DB_SCHEMA")
        if schema != "baianas_moda_feminina":
            raise RuntimeError("Seed destinado apenas ao banco isolado baianas_moda_feminina")
        print("Schema:", schema)
        print("Categorias planejadas:", len(CATEGORIES), "Produtos planejados:", len(PRODUCTS))
        if not args.apply:
            for slug, _cat, name, price, _color, stocks, _photo in PRODUCTS:
                print(f"  {PREFIX}{slug}: {name} | R$ {price} | estoque: {sum(stocks)}")
            print("PREVIEW: nenhum dado gravado. Use --apply para inserir.")
            return
        created_categories = 0
        created_products = 0
        try:
            categories = {}
            for slug, name in CATEGORIES:
                category = Category.query.filter_by(slug=PREFIX + slug).first()
                if category is None:
                    category = Category(slug=PREFIX + slug, name=name + " [DEMO]", source="internal")
                    db.session.add(category)
                    db.session.flush()
                    created_categories += 1
                categories[slug] = category
            for slug, cat, name, price, color, stocks, photo in PRODUCTS:
                external_id = PREFIX + slug
                if Product.query.filter_by(external_id=external_id).first():
                    continue
                image = IMAGES[photo]
                p = Product(
                    external_id=external_id, source="internal",
                    name=name,
                    description="PRODUTO FICTÍCIO PARA HOMOLOGAÇÃO. Foto meramente ilustrativa; preço, cor, características e estoque são exemplos. Não disponível para venda real.",
                    price=Decimal(price), image1=image, category=categories[cat].name,
                    category_id=categories[cat].id, stock=sum(stocks),
                    is_active=True, is_draft=False,
                    currency="BRL",
                )
                db.session.add(p)
                db.session.flush()
                db.session.add(ProductImage(product_id=p.id, url=image, alt="Foto ilustrativa de moda — " + name, position=0, is_primary=True))
                for size, count in zip(("P", "M", "G"), stocks):
                    db.session.add(ProductVariant(
                        product_id=p.id,
                        sku=f"DEMO-{slug.upper()}-{size}",
                        color=color, size=size, stock=count, price=Decimal(price),
                        image_url=image, is_active=True,
                    ))
                created_products += 1
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        print(f"INSERIDO: {created_categories} categorias, {created_products} produtos (3 variantes cada).")
        print("ATENÇÃO: registros estão visíveis no catálogo. Não habilite pagamentos reais nem anuncie produtos fictícios.")

if __name__ == "__main__":
    main()
