"""Reconhecimento de cor/tamanho em attributes de variante (caso real).

Caso real: produto "Calça Cargo Feminina" (nome editado manualmente —
preservado em todos os fluxos) com 18 variantes de size=NULL; os tamanhos
vivem em attributes sob "Tamanho infantil dos EUA" (sku_property_id
200000463): 5T(Size 120)…13-14T(Size 170). Antes da correção, variantes da
mesma cor pareciam iguais e recebiam rótulos genéricos "Opção N".

Cobre, com dados simulados (sem chamadas reais ao AliExpress):
- 12 combinações ativas cor/tamanho, exclusão das inativas, sem "Opção N";
- correspondência exata seleção -> variant_id/SKU (sem auto-seleção);
- rótulos completos na página, carrinho e checkout;
- snapshot completo no pedido; nome manual preservado;
- aliases existentes, atributos ausentes, colisões e limites de coluna.
"""

import uuid
from decimal import Decimal

import pytest

from loja.modules.models import Cart, Product, ProductVariant
from loja.views.routes import (
    _assert_selector_invariant,
    _build_variant_selector,
    _resolve_selected_variant,
)

SIZES_REAIS = [
    "5T(Size 120)",
    "6T(Size 130)",
    "7-8T(Size 140)",
    "9-10T(Size 150)",
    "11-12T(Size 160)",
    "13-14T(Size 170)",
]
CORES_ATIVAS = ["Grey", "Purple"]
NOME_MANUAL = "Calça Cargo Feminina"


@pytest.fixture(autouse=True)
def _cleanup_db(db):
    yield
    db.session.rollback()
    db.session.remove()


def _attrs(cor, tamanho):
    return [
        {"name": "cor", "value": cor, "sku_property_id": "14"},
        {
            "name": "Tamanho infantil dos EUA",
            "value": tamanho,
            "sku_property_id": "200000463",
        },
    ]


def make_cargo(db, *, name=NOME_MANUAL, active_colors=CORES_ATIVAS):
    """Replica o caso real: size=NULL, tamanhos só em attributes."""
    product = Product(
        id=uuid.uuid4(),
        name=name,
        description="<p>Calça cargo feminina com bolsos laterais.</p>",
        price=Decimal("119.90"),
        stock=0,
        image1="https://exemplo.com/cargo.jpg",
        category="feminino",
        is_active=True,
        is_draft=False,
        source="aliexpress",
        external_id="1005012000378696",
        currency="BRL",
        pricing_status="priced",
    )
    db.session.add(product)
    db.session.flush()
    variantes = {}
    for cor in ["Black", "Grey", "Purple"]:
        for tamanho in SIZES_REAIS:
            ativa = cor in active_colors
            v = ProductVariant(
                id=uuid.uuid4(),
                product_id=product.id,
                external_sku_id=f"cargo-{cor}-{tamanho}",
                supplier_sku_id=f"cargo-{cor}-{tamanho}",
                color=cor,
                size=None,  # caso real: coluna normalizada ausente
                attributes=_attrs(cor, tamanho),
                stock=5 if ativa else 0,
                price=Decimal("80.00"),
                sale_price=None,
                is_active=ativa,
            )
            db.session.add(v)
            variantes[(cor, tamanho)] = v
    db.session.commit()
    db.session.refresh(product)
    return product, variantes


@pytest.fixture()
def cargo(db):
    return make_cargo(db)


def _login(client, make_user):
    user = make_user()
    client.post("/login", data={"email": user.email, "password": "senha123"})
    return user


def test_page_mostra_12_combinacoes_sem_opcao(client, db, cargo):
    product, variantes = cargo
    resp = client.get(f"/produtos/{product.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    # Título usa o nome comercial editado manualmente.
    assert NOME_MANUAL in body
    assert '<h1 class="product-name">' in body

    # 12 combinações ativas com rótulos completos.
    for cor in CORES_ATIVAS:
        for tamanho in SIZES_REAIS:
            assert tamanho in body, f"tamanho ausente na página: {tamanho}"
    # Nenhum rótulo genérico e nenhuma variante inativa.
    assert "Opção" not in body
    assert "Black" not in body

    selector = _build_variant_selector(product)
    _assert_selector_invariant(selector)
    assert selector["uses_variants"] is True
    assert len(selector["variants"]) == 12
    assert {tuple(v["dims"]) for v in selector["variants"]} == {
        (cor, tamanho) for cor in CORES_ATIVAS for tamanho in SIZES_REAIS
    }


def test_selecao_casa_exactamente_com_variant_id_e_sku(client, db, cargo):
    product, variantes = cargo
    for (cor, tamanho), variante in variantes.items():
        if cor not in CORES_ATIVAS:
            continue
        resolvida = _resolve_selected_variant(product, str(variante.id))
        assert resolvida is not None
        assert resolvida.id == variante.id
        assert resolvida.supplier_sku_id == f"cargo-{cor}-{tamanho}"

    # Sem variant_id não há auto-seleção do primeiro SKU.
    with pytest.raises(ValueError):
        _resolve_selected_variant(product, None)
    # UUID inválido é rejeitado.
    with pytest.raises(ValueError):
        _resolve_selected_variant(product, "nao-um-uuid")
    # Variante inativa (Black, sem estoque) é rejeitada: sem auto-seleção.
    with pytest.raises(ValueError):
        _resolve_selected_variant(
            product, str(variantes[("Black", SIZES_REAIS[0])].id)
        )


def test_carrinho_e_checkout_exibem_rotulos_completos(
    db, client, make_user, cargo
):
    product, variantes = cargo
    user = _login(client, make_user)

    grey_5t = variantes[("Grey", "5T(Size 120)")]
    resp = client.post(
        f"/produtos/{product.id}/processar",
        data={
            "action": "add_to_cart",
            "variant_id": str(grey_5t.id),
            "quantity": "1",
        },
        follow_redirects=True,
    )
    assert "adicionado ao carrinho" in resp.get_data(as_text=True).lower()

    cart = Cart.query.filter_by(user_id=user.id).first()
    items = list(cart.items)
    assert len(items) == 1
    assert items[0].variant_id == grey_5t.id
    # Sem truncamento silencioso: rótulo completo (11 chars > VARCHAR(10)).
    assert items[0].selected_size == "5T(Size 120)"
    assert items[0].product.name == NOME_MANUAL

    pagina = client.get("/cart").get_data(as_text=True)
    assert "Grey" in pagina
    assert "5T(Size 120)" in pagina

    # Checkout direto (buy_now) identifica cor + tamanho completos.
    purple = variantes[("Purple", "11-12T(Size 160)")]
    checkout = client.post(
        f"/produtos/{product.id}/processar",
        data={
            "action": "buy_now",
            "variant_id": str(purple.id),
            "quantity": "1",
        },
        follow_redirects=True,
    )
    corpo = checkout.get_data(as_text=True)
    assert checkout.status_code == 200
    assert "Purple" in corpo
    assert "11-12T(Size 160)" in corpo
    assert NOME_MANUAL in corpo


def test_snapshot_do_pedido_com_tamanho_completo(db, cargo):
    from loja.modules.getway_mercado_pago.payment_models import OrderItem
    from loja.views import payments_routes as pr

    product, variantes = cargo
    variante = variantes[("Grey", "13-14T(Size 170)")]
    resolvida, rotulo = pr._resolve_payment_variant(
        product, str(variante.id), "IGNORADO"
    )
    assert resolvida.id == variante.id
    # Rótulo completo (16 chars > VARCHAR(10 legado), cabe no snapshot).
    assert rotulo == "13-14T(Size 170)"

    item = OrderItem(
        id=uuid.uuid4(),
        order_id=uuid.uuid4(),
        product_id=product.id,
        product_name=product.name,
        selected_size=rotulo,
        variant_id=variante.id,
        quantity=1,
        unit_price=product.price,
        line_total=product.price,
    )
    db.session.add(item)
    db.session.commit()
    assert item.selected_size == "13-14T(Size 170)"
    assert item.product_name == NOME_MANUAL
    assert item.variant_id == variante.id


def test_rotulo_longo_acima_do_teto_falha_explicito(db, client, make_user):
    # 41 chars > VARIANT_LABEL_MAX_LEN (40): falha explícita, sem truncar e
    # sem criar item no carrinho.
    product = Product(
        id=uuid.uuid4(),
        name="Tamanho Longo",
        description="x",
        price=Decimal("10.00"),
        stock=5,
        is_active=True,
        is_draft=False,
        source="aliexpress",
        pricing_status="priced",
    )
    db.session.add(product)
    db.session.flush()
    variante = ProductVariant(
        id=uuid.uuid4(),
        product_id=product.id,
        external_sku_id="sku-long",
        supplier_sku_id="sku-long",
        color="Azul",
        size="X" * 41,
        stock=3,
        price=Decimal("10.00"),
        is_active=True,
    )
    db.session.add(variante)
    db.session.commit()
    user = _login(client, make_user)

    resp = client.post(
        f"/produtos/{product.id}/processar",
        data={
            "action": "add_to_cart",
            "variant_id": str(variante.id),
            "quantity": "1",
        },
        follow_redirects=True,
    )
    assert "excede o limite" in resp.get_data(as_text=True).lower()

    cart = Cart.query.filter_by(user_id=user.id).first()
    assert cart is None or list(cart.items) == []


def test_aliases_unificados_e_property_ids():
    from loja.services.variant_attributes import (
        classify_variant_attribute as cls,
        extract_variant_color_size as ext,
    )

    # Aliases preexistentes (importador + apresentação unificados).
    assert cls({"name": "Size", "value": "M"}) == "size"
    assert cls({"name": "TAMANHO", "value": "G"}) == "size"
    assert cls({"name": "talla", "value": "L"}) == "size"
    assert cls({"name": "Cor", "value": "Rosa"}) == "color"
    assert cls({"name": "COLOUR", "value": "Blue"}) == "color"
    # Atributo do caso real, por nome e por property id.
    assert (
        cls({"name": "Tamanho infantil dos EUA", "value": "5T(Size 120)"})
        == "size"
    )
    assert (
        cls(
            {
                "name": "Kids US Size",
                "value": "5T",
                "sku_property_id": "200000463",
            }
        )
        == "size"
    )
    assert cls({"name": "cor", "value": "Grey"}) == "color"
    # Desconhecidos nunca classificados (nem por nome nem por id).
    assert cls({"name": "Material", "value": "Algodão"}) is None
    assert (
        cls({"name": "Estampa", "value": "X", "sku_property_id": "999999"})
        is None
    )
    assert cls("nao-um-dict") is None
    # Classificação é do atributo; valor vazio é filtrado na extração.
    assert cls({"name": "size", "value": "  "}) == "size"
    assert ext([{"name": "size", "value": "  "}]) == (None, None)
    # Extração fim a fim no formato do importer.
    cor, tamanho = ext(
        [
            {"name": "cor", "value": "Grey", "sku_property_id": "14"},
            {
                "name": "Tamanho infantil dos EUA",
                "value": "5T(Size 120)",
                "sku_property_id": "200000463",
            },
        ]
    )
    assert (cor, tamanho) == ("Grey", "5T(Size 120)")


def test_importador_extrai_tamanho_do_caso_real():
    from loja.integrations.aliexpress.importer import _extract_variant_meta

    cor, tamanho = _extract_variant_meta(
        [
            {"name": "cor", "value": "Purple", "sku_property_id": "14"},
            {
                "name": "Tamanho infantil dos EUA",
                "value": "9-10T(Size 150)",
                "sku_property_id": "200000463",
            },
        ]
    )
    assert (cor, tamanho) == ("Purple", "9-10T(Size 150)")


def test_atributos_ausentes_mantem_opcao_e_sem_autoselecao(client, db):
    product = Product(
        id=uuid.uuid4(),
        name="Sem Atributos",
        description="Sem cor nem tamanho.",
        price=Decimal("20.00"),
        stock=2,
        is_active=True,
        is_draft=False,
        source="aliexpress",
        pricing_status="priced",
    )
    db.session.add(product)
    db.session.flush()
    for i in range(2):
        db.session.add(
            ProductVariant(
                id=uuid.uuid4(),
                product_id=product.id,
                external_sku_id=f"sem-attr-{i}",
                supplier_sku_id=f"sem-attr-{i}",
                color=None,
                size=None,
                attributes=[],
                stock=1,
                price=Decimal("20.00"),
                is_active=True,
            )
        )
    db.session.commit()

    selector = _build_variant_selector(product)
    _assert_selector_invariant(selector)
    # Tratamento explícito preservado: rótulos genéricos, sem travar a página.
    assert any("Opção" in opt for dim in selector["dimensions"] for opt in dim["options"])
    pagina = client.get(f"/produtos/{product.id}")
    assert pagina.status_code == 200
    # Sem variant_id não há resolução automática para o primeiro SKU.
    with pytest.raises(ValueError):
        _resolve_selected_variant(product, None)


def test_colisao_continua_desambiguada(db, cargo):
    from loja.views.routes import _build_variant_selector as build

    product, variantes = cargo
    # Força colisão: duas variantes Grey com o mesmo tamanho.
    gemea = variantes[("Grey", "5T(Size 120)")]
    gemea.attributes = list(variantes[("Purple", "5T(Size 120)")].attributes)
    gemea.color = "Purple"
    gemea.size = None
    db.session.commit()

    selector = build(product)
    _assert_selector_invariant(selector)
    # Desempate explícito via dimensão extra; combinações continuam únicas.
    tuplas = [tuple(v["dims"]) for v in selector["variants"]]
    assert len(tuplas) == len(set(tuplas))
    assert any(d["key"] == "option" for d in selector["dimensions"])
    # Cada combinação resolve exatamente para o seu variant_id.
    for variante in selector["variants"]:
        resolvida = _resolve_selected_variant(product, variante["id"])
        assert str(resolvida.id) == variante["id"]


def test_nome_manual_preservado_nos_fluxos(db, client, make_user, cargo):
    product, variantes = cargo
    assert product.name == NOME_MANUAL

    pagina = client.get(f"/produtos/{product.id}").get_data(as_text=True)
    assert f"<title>{NOME_MANUAL}" in pagina or NOME_MANUAL in pagina

    user = _login(client, make_user)
    client.post(
        f"/produtos/{product.id}/processar",
        data={
            "action": "add_to_cart",
            "variant_id": str(variantes[("Grey", "6T(Size 130)")].id),
            "quantity": "1",
        },
        follow_redirects=True,
    )
    cart = Cart.query.filter_by(user_id=user.id).first()
    item = list(cart.items)[0]
    assert item.product.name == NOME_MANUAL
    assert "1005012000378696" not in pagina  # sem vazamento de SKU externo
