"""Visibilidade pública (Fase 6): apenas produtos ativos E não-rascunho
aparecem na vitrine virtual/loja; rascunho nunca vaza mesmo com preço,
imagem e estoque definidos, e detalhe direto retorna 404.
"""
import uuid
from decimal import Decimal

from loja.integrations.aliexpress.importer import SOURCE_ALIEXPRESS
from loja.modules.models import Product


def _pub(db, label, *, is_active, is_draft, price="99.90", stock=5):
    name = f"{label} {uuid.uuid4().hex[:6]}"
    product = Product(
        id=uuid.uuid4(),
        name=name,
        description="desc",
        price=Decimal(price),
        image1="https://exemplo.com/vaza.png",
        stock=stock,
        is_active=is_active,
        is_draft=is_draft,
        source="internal" if not is_draft else SOURCE_ALIEXPRESS,
        external_id=None if not is_draft else "1005" + uuid.uuid4().hex[:12],
    )
    db.session.add(product)
    db.session.commit()
    return product


def _seed(db):
    return {
        "visivel": _pub(db, "Camiseta Visivel", is_active=True, is_draft=False),
        "draft": _pub(db, "Blusa Draft", is_active=False, is_draft=True),
        "inativo": _pub(db, "Vestido Inativo", is_active=False, is_draft=False),
        "draft_inativo": _pub(db, "Saia Draft Inativo", is_active=False, is_draft=True),
    }


def test_only_active_non_draft_appears_in_listing(app, client, db):
    seed = _seed(db)
    r = client.get("/produtos?ordenar=recentes")
    assert r.status_code == 200
    body = r.data.decode()
    assert seed["visivel"].name in body
    assert seed["draft"].name not in body
    assert seed["inativo"].name not in body
    assert seed["draft_inativo"].name not in body


def test_draft_never_leaks_even_with_price_image_and_stock(app, client, db):
    # Estado legal de um rascunho AliExpress: inactive + draft. Mesmo com
    # preço, imagem e estoque preenchidos, NÃO pode vazar.
    draft = _pub(
        db, "Blusa Draft Leak", is_active=False, is_draft=True,
        price="999.99", stock=99,
    )
    r = client.get(f"/produtos/{draft.id}")
    assert r.status_code == 404

    r = client.get("/produtos?ordenar=recentes")
    assert draft.name not in r.data.decode()


def test_inactive_non_draft_details_are_404(app, client, db):
    seed = _seed(db)
    r = client.get(f"/produtos/{seed['inativo'].id}")
    assert r.status_code == 404


def test_visible_product_details_200(app, client, db):
    seed = _seed(db)
    r = client.get(f"/produtos/{seed['visivel'].id}")
    assert r.status_code == 200


def test_search_only_returns_public(app, client, db):
    seed = _seed(db)
    r = client.get(f"/produtos?q={seed['visivel'].name}")
    assert r.status_code == 200
    assert seed["visivel"].name in r.data.decode()

    r = client.get(f"/produtos?q={seed['draft'].name}")
    assert r.status_code == 200
    assert seed["draft"].name not in r.data.decode()


def test_public_product_query_uses_active_and_not_draft(app, db):
    seed = _seed(db)
    from loja.modules.services import public_product_query

    ids = {p.id for p in public_product_query().all()}
    assert seed["visivel"].id in ids
    assert seed["draft"].id not in ids
    assert seed["inativo"].id not in ids
    assert seed["draft_inativo"].id not in ids