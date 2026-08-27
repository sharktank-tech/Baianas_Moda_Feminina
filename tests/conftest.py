import os
import tempfile
import uuid

# Captura a URL original fornecida pelo usuário ANTES de sobrescrevê-la com
# SQLite. Os testes de isolamento de schema (tests/test_schema_isolation.py)
# usam ORIGINAL_DATABASE_URL para enxergar o PostgreSQL real; a suíte comum
# continua isolada em SQLite.
ORIGINAL_DATABASE_URL = os.environ.get("DATABASE_URL", "")

# A classe Config do projeto é avaliada no primeiro import e exige DATABASE_URL.
# Para os testes usamos um SQLite em arquivo temporário (ver compilers abaixo).
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP_DB.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.name}"
os.environ["SECRET_KEY"] = "test-secret-key"

from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB, UUID  # noqa: E402


# Os models usam tipos PostgreSQL. Para permitir rodar os testes em SQLite,
# registramos compilers de DDL específicos para o dialeto sqlite.
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "VARCHAR(36)"


import pytest  # noqa: E402


def unique_email(prefix="cliente"):
    return f"{prefix}-{uuid.uuid4().hex[:8]}@teste.com"


@pytest.fixture(scope="session")
def app():
    from loja import create_app
    from loja import db

    test_app = create_app()
    test_app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
    )

    with test_app.app_context():
        db.create_all()

    yield test_app

    with test_app.app_context():
        db.session.remove()
        db.drop_all()

    if os.path.exists(_TMP_DB.name):
        os.unlink(_TMP_DB.name)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db(app):
    from loja import db
    with app.app_context():
        yield db


# ── Dados de apoio ────────────────────────────────────────────────────────────

@pytest.fixture()
def make_user(db):
    def _make(email=None, password="senha123", is_admin=False):
        from loja.modules.models import User

        user = User(
            id=uuid.uuid4(),
            name=(email or unique_email()).split("@")[0],
            email=email or unique_email(),
            phone="11999999999",
            is_admin=is_admin,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return user

    return _make


@pytest.fixture()
def admin_user(make_user):
    return make_user(email=unique_email("admin"), is_admin=True)


@pytest.fixture()
def regular_user(make_user):
    return make_user()


@pytest.fixture()
def make_product(db):
    def _make(name="Camiseta Básica", price="49.90", stock=10, category="masculino"):
        from decimal import Decimal
        from loja.modules.models import Product

        product = Product(
            id=uuid.uuid4(),
            name=name,
            description=f"Descrição do produto {name}",
            price=Decimal(price),
            image1="https://exemplo.com/img.png",
            category=category,
            stock=stock,
            is_active=True,
        )
        db.session.add(product)
        db.session.commit()
        return product

    return _make


@pytest.fixture()
def product(make_product):
    return make_product()


@pytest.fixture()
def make_coupon(db):
    def _make(
        code=None,
        discount_type="percentage",
        discount_value="10",
        first_purchase_only=False,
        is_active=True,
    ):
        from decimal import Decimal
        from loja.modules.models import Coupon

        coupon = Coupon(
            id=uuid.uuid4(),
            code=code or unique_email("CUPOM").replace("@", "").upper()[:12],
            description="Cupom de teste",
            discount_type=discount_type,
            discount_value=Decimal(discount_value),
            minimum_order_amount=Decimal("0.00"),
            times_used=0,
            first_purchase_only=first_purchase_only,
            is_active=is_active,
        )
        db.session.add(coupon)
        db.session.commit()
        return coupon

    return _make


@pytest.fixture()
def login(client):
    def _login(email, password="senha123"):
        return client.post(
            "/login",
            data={"email": email, "password": password},
            follow_redirects=True,
        )

    return _login