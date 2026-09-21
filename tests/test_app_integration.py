import re
import uuid
from decimal import Decimal
from pathlib import Path

from conftest import unique_email


# ── Estrutura da aplicação ────────────────────────────────────────────────────

def test_home_page_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Pollo" in resp.get_data(as_text=True)


def test_blueprints_registered(app):
    rules = {rule.endpoint for rule in app.url_map.iter_rules()}
    assert "main.home" in rules
    assert "main.produtos" in rules
    assert "admin.admin_dashboard" in rules
    assert "payments.create_mercado_pago_payment" in rules
    assert "tracking.acompanhar_pedido" in rules


def test_payment_webhook_and_return_registered(app):
    """Regressão: o webhook e o retorno precisam existir (fluxo de pagamento)."""
    rules = {rule.endpoint for rule in app.url_map.iter_rules()}
    assert "payments.mercado_pago_webhook" in rules
    assert "payments.payment_return" in rules


def test_payment_return_requires_login_and_renders(client, login, make_user):
    resp = client.get("/pagamentos/retorno/sucesso")
    assert resp.status_code == 302

    user = make_user()
    login(user.email)
    resp = client.get("/pagamentos/retorno/sucesso")
    assert resp.status_code == 200


def test_all_rendered_templates_exist():
    """Regressão: qualquer template referenciado pelas views precisa existir."""
    missing = []
    for view_path in (
        "loja/views/routes.py",
        "loja/views/admin_routes.py",
        "loja/views/payments_routes.py",
    ):
        src = Path(view_path).read_text()
        for template in re.findall(
            r"render_template\(\s*[\"']([^\"']+)[\"']", src
        ):
            if not Path("loja/templates", template).exists():
                missing.append(template)
    assert missing == [], f"Templates faltando: {missing}"


def test_tracking_service_imports():
    import loja.modules.tracking_service  # noqa: F401


# ── Fluxo de usuário ──────────────────────────────────────────────────────────

def test_register_login_and_account(client, db):
    email = unique_email()
    resp = client.post(
        "/register",
        data={
            "username": "Novo Cliente",
            "email": email,
            "telefone": "11988887777",
            "password": "senha123",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    resp = client.post(
        "/login",
        data={"email": email, "password": "senha123"},
        follow_redirects=True,
    )
    assert "minha conta" in resp.get_data(as_text=True).lower() or resp.status_code == 200

    resp = client.get("/conta")
    assert resp.status_code == 200


def test_add_to_cart_and_manage(client, db, product, make_user):
    user = make_user()
    client.post(
        "/login",
        data={"email": user.email, "password": "senha123"},
    )

    resp = client.post(
        f"/produtos/{product.id}/processar",
        data={"action": "add_to_cart", "size": "M", "quantity": 2},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    from loja.modules.models import CartItem

    item = CartItem.query.filter_by(product_id=product.id).first()
    assert item is not None
    assert item.quantity == 2
    assert item.selected_size == "M"

    resp = client.get("/cart")
    assert resp.status_code == 200
    assert product.name in resp.get_data(as_text=True)

    resp = client.post(
        f"/cart/item/{item.id}/quantidade",
        data={"action": "increase"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert item.quantity == 3


def test_remove_from_cart(client, db, product, make_user):
    user = make_user()
    client.post(
        "/login",
        data={"email": user.email, "password": "senha123"},
    )
    client.post(
        f"/produtos/{product.id}/processar",
        data={"action": "add_to_cart", "size": "P", "quantity": 1},
    )

    from loja.modules.models import CartItem

    item = CartItem.query.filter_by(product_id=product.id).first()
    resp = client.post(
        f"/cart/item/{item.id}/remover",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert CartItem.query.filter_by(id=item.id).first() is None


def test_buy_now_checkout(client, db, product, make_user):
    user = make_user()
    client.post(
        "/login",
        data={"email": user.email, "password": "senha123"},
    )
    resp = client.post(
        f"/produtos/{product.id}/processar",
        data={"action": "buy_now", "size": "G", "quantity": 1},
        follow_redirects=True,
    )
    assert resp.status_code == 200


def test_favorite_flow(client, product, make_user):
    user = make_user()
    client.post(
        "/login",
        data={"email": user.email, "password": "senha123"},
    )
    resp = client.post(f"/produtos/{product.id}/favoritar")
    assert resp.status_code == 200
    assert resp.get_json() == {"is_favorited": True}

    resp = client.post(f"/produtos/{product.id}/favoritar")
    assert resp.get_json() == {"is_favorited": False}


# ── Cupons ────────────────────────────────────────────────────────────────────

def test_apply_coupon_in_checkout(client, db, product, make_user, make_coupon):
    user = make_user()
    client.post(
        "/login",
        data={"email": user.email, "password": "senha123"},
    )
    coupon = make_coupon(code="DESCONTO10", discount_value="10")

    resp = client.post(
        "/checkout/aplicar-cupom",
        json={
            "code": "desconto10",
            "checkout_type": "product",
            "product_id": str(product.id),
            "quantity": 1,
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert abs(float(data["discount"]) - 4.99) < 0.01


def test_first_purchase_coupon_blocked_for_prior_buyer(
    client, db, product, make_user, make_coupon, monkeypatch
):
    """Regressão do bypass: usuário com compra anterior não usa cupom 1ª compra."""
    import loja.views.payments_routes as pr

    from loja.modules.getway_mercado_pago.payment_models import Order

    # Cotação simulada: o foco aqui é o cupom, não a tarifa.
    monkeypatch.setattr(pr, "quote_checkout_shipping", lambda item_data, cep: Decimal("18.90"))

    user = make_user()
    client.post(
        "/login",
        data={"email": user.email, "password": "senha123"},
    )
    from test_shipping_quote import preset_shipping_quote

    preset_shipping_quote(
        client, "01310100",
        [{"product_id": product.id, "quantity": 1}],
        "18.90",
    )
    coupon = make_coupon(
        code="PRIMEIRACOMPRA",
        discount_value="10",
        first_purchase_only=True,
    )

    form = {
        "checkout_type": "product",
        "product_id": str(product.id),
        "quantity": 1,
        "selected_size": "M",
        "coupon_code": coupon.code,
        "nome": "Cliente Teste",
        "email": user.email,
        "cep": "01310100",
        "estado": "SP",
        "endereco": "Rua Teste",
        "numero": "10",
        "bairro": "Centro",
        "cidade": "São Paulo",
    }

    # Sem compra anterior: cupom passa na validação (falha depois, sem token MP).
    resp = client.post("/pagamentos/mercado-pago", data=form, follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "exclusivo" not in body.lower()
    assert "não foi possível abrir o pagamento" in body.lower()

    # Cria uma compra paga para o usuário.
    db.session.add(
        Order(
            id=uuid.uuid4(),
            user_id=user.id,
            checkout_type="product",
            status="paid",
            subtotal=Decimal("49.90"),
            shipping=Decimal("18.90"),
            discount=Decimal("0.00"),
            total=Decimal("68.80"),
            customer_name="Cliente Teste",
            customer_email=user.email,
            zip_code="01310100",
            street="Rua Teste",
            number="10",
            neighborhood="Centro",
            city="São Paulo",
            state="SP",
        )
    )
    db.session.commit()

    # Com compra anterior: cupom de primeira compra deve ser bloqueado.
    resp = client.post("/pagamentos/mercado-pago", data=form, follow_redirects=True)
    assert "exclusivo para a primeira compra" in resp.get_data(as_text=True).lower()


# ── Área administrativa ───────────────────────────────────────────────────────

def test_admin_dashboard_requires_admin(client, admin_user, make_user):
    regular = make_user()
    client.post(
        "/login",
        data={"email": regular.email, "password": "senha123"},
    )
    resp = client.get("/admin", follow_redirects=True)
    assert "permissão" in resp.get_data(as_text=True).lower()


def test_admin_pages_render(client, db, admin_user, make_user, make_product,
                            make_coupon):
    from loja.modules.models import (
        SiteConfig, FooterConfig, ContactInfo, SocialLink, NavLink,
        CategoryLink, LegalLink, Supplier, Testimonial, HeroSlide, Promotion,
    )

    client.post(
        "/login",
        data={"email": admin_user.email, "password": "senha123"},
    )

    # Singletons exigidos pelo dashboard. SiteConfig é criado no POST abaixo
    # para validar também o template add_site_config.html.
    db.session.add_all([
        FooterConfig(id=uuid.uuid4(), brand_name="Pollo Modas"),
        ContactInfo(id=uuid.uuid4(), email="contato@teste.com"),
    ])
    product = make_product()
    supplier = Supplier(id=uuid.uuid4(), name="Fornecedor Teste")
    db.session.add(supplier)
    testimonial = Testimonial(id=uuid.uuid4(), author="Autor", text="Texto")
    db.session.add(testimonial)
    hero = HeroSlide(id=uuid.uuid4(), image="https://exemplo.com/hero.png")
    db.session.add(hero)
    social = SocialLink(id=uuid.uuid4(), label="Instagram", url="https://ig")
    db.session.add(social)
    db.session.add_all([
        NavLink(id=uuid.uuid4(), label="Sobre", url="/sobre"),
        CategoryLink(id=uuid.uuid4(), label="Feminino", url="/produtos",
                     slug="feminino", image="https://exemplo.com/cat.png"),
        LegalLink(id=uuid.uuid4(), label="Privacidade", url="/politica-privacidade"),
    ])
    db.session.commit()
    promo = Promotion(
        id=uuid.uuid4(),
        product_id=product.id,
        sale_price=Decimal("39.90"),
        is_active=True,
    )
    db.session.add(promo)
    db.session.commit()

    other_user = make_user()
    coupon = make_coupon()

    # Template novo (regressão): renderiza apenas quando não há SiteConfig.
    resp = client.get("/admin/add_site_config")
    assert resp.status_code == 200, f"GET /admin/add_site_config retornou {resp.status_code}"

    # Cria o singleton via POST.
    resp = client.post(
        "/admin/add_site_config",
        data={"name": "Pollo Modas Teste", "copyright_year": "2026"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    pages = [
        "/admin",
        "/admin/products/add",
        f"/admin/products/edit/{product.id}",
        "/admin/add_promotion",
        f"/admin/edit_promotion/{promo.id}",
        "/admin/coupons/add",
        f"/admin/coupons/edit/{coupon.id}",
        "/admin/add_supplier",
        f"/admin/edit_supplier/{supplier.id}",
        "/admin/add_testimonial",
        f"/admin/edit_testimonial/{testimonial.id}",
        "/admin/add_hero_slide",
        f"/admin/edit_hero_slide/{hero.id}",
        "/admin/edit_site_config",
        "/admin/add_social_link",
        f"/admin/edit_social_link/{social.id}",
        "/admin/edit_footer_config",
        "/admin/edit_contact_info",
        "/admin/add_nav_link",
        f"/admin/edit_nav_link/{NavLink.query.first().id}",
        "/admin/add_category_link",
        f"/admin/edit_category_link/{CategoryLink.query.first().id}",
        "/admin/add_legal_link",
        f"/admin/edit_legal_link/{LegalLink.query.first().id}",
        "/admin/users/add",
        f"/admin/users/edit/{other_user.id}",
    ]

    for url in pages:
        resp = client.get(url)
        assert resp.status_code == 200, f"GET {url} retornou {resp.status_code}"


# ── CLI ───────────────────────────────────────────────────────────────────────

def test_create_admin_cli(app, db):
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "create-admin",
            "--email", "admin-cli@teste.com",
            "--password", "segredo123",
            "--name", "Admin CLI",
        ],
    )
    assert result.exit_code == 0
    assert "criado com sucesso" in result.output

    from loja.modules.models import User
    user = User.query.filter_by(email="admin-cli@teste.com").first()
    assert user is not None
    assert user.is_admin is True

    # Criar de novo não deve duplicar.
    result = runner.invoke(
        args=[
            "create-admin",
            "--email", "admin-cli@teste.com",
            "--password", "segredo123",
        ],
    )
    assert "já existe" in result.output
    assert User.query.filter_by(email="admin-cli@teste.com").count() == 1


# ── Rastreio ──────────────────────────────────────────────────────────────────

def test_tracking_page_renders(client):
    resp = client.get("/acompanhar-pedido/")
    assert resp.status_code == 200