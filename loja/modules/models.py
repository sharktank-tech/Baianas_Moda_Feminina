import uuid
from datetime import datetime
from loja import db
from flask_login import UserMixin
from sqlalchemy.dialects.postgresql import UUID
from werkzeug.security import generate_password_hash, check_password_hash


# ==========================================================
# Associação Produto <-> Fornecedor (N:N)
# ==========================================================
product_supplier = db.Table(
    "product_supplier",

    db.Column(
        "product_id",
        UUID(as_uuid=True),
        db.ForeignKey("products.id"),
        primary_key=True
    ),

    db.Column(
        "supplier_id",
        UUID(as_uuid=True),
        db.ForeignKey("suppliers.id"),
        primary_key=True
    )
)


# ==========================================================
# Configurações do site
# ==========================================================
class SiteConfig(db.Model):
    __tablename__ = "site_config"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )

    name = db.Column(db.String(150), nullable=False)
    meta_description = db.Column(db.Text)
    banner_quote = db.Column(db.Text)
    banner_cta_text = db.Column(db.String(100))
    promo_badge = db.Column(db.String(100))
    copyright_year = db.Column(db.Integer)
    copyright_text = db.Column(db.String(255))


# ==========================================================
# Hero Slides
# ==========================================================
class HeroSlide(db.Model):
    __tablename__ = "hero_slides"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    image = db.Column(db.Text, nullable=False)
    alt = db.Column(db.String(255))
    eyebrow = db.Column(db.String(100))
    title = db.Column(db.String(100))

# ==========================================================
# Fornecedores
# ==========================================================
class Supplier(db.Model):
    __tablename__ = "suppliers"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    name = db.Column(db.String(150), nullable=False)
    company_name = db.Column(db.String(200))
    phone = db.Column(db.String(30))
    email = db.Column(db.String(120))
    website = db.Column(db.String(255))
    address = db.Column(db.Text)
    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    products = db.relationship(
        "Product",
        secondary=product_supplier,
        back_populates="suppliers",
        lazy=True
    )

# ==========================================================
# Produtos
# ==========================================================
class Product(db.Model):
    __tablename__ = "products"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    price = db.Column(
        db.Numeric(10, 2),
        nullable=False
    )
    image1 = db.Column(db.Text)
    image2 = db.Column(db.Text)
    image3 = db.Column(db.Text)
    image4 = db.Column(db.Text)
    category = db.Column(db.String(100))
    stock = db.Column(
        db.Integer,
        default=0
    )

    is_active = db.Column(
        db.Boolean,
        default=True
    )
    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )
    suppliers = db.relationship(
        "Supplier",
        secondary=product_supplier,
        back_populates="products",
        lazy=True
    )

    def __repr__(self):
        return f"<Product {self.name}>"

# ==========================================================
# Promoções
# ==========================================================
class Promotion(db.Model):
    __tablename__ = "promotions"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    name = db.Column(db.String(255))
    image = db.Column(db.Text)
    original_price = db.Column(
        db.Numeric(10, 2)
    )
    sale_price = db.Column(
        db.Numeric(10, 2)
    )
    is_active = db.Column(
        db.Boolean,
        default=True
    )


# ==========================================================
# Depoimentos
# ==========================================================
class Testimonial(db.Model):
    __tablename__ = "testimonials"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    author = db.Column(db.String(150))
    text = db.Column(db.Text)
    rating = db.Column(db.Integer)
    is_visible = db.Column(
        db.Boolean,
        default=True
    )

# ==========================================================
# Rodapé
# ==========================================================
class FooterConfig(db.Model):
    __tablename__ = "footer_config"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    brand_name = db.Column(
        db.String(150),
        nullable=False
    )
    brand_description = db.Column(db.Text)

# ==========================================================
# Redes sociais
# ==========================================================
class SocialLink(db.Model):
    __tablename__ = "social_links"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    label = db.Column(
        db.String(50),
        nullable=False
    )
    url = db.Column(db.Text)
    icon = db.Column(db.String(50))

# ==========================================================
# Links de navegação
# ==========================================================
class NavLink(db.Model):
    __tablename__ = "nav_links"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    label = db.Column(
        db.String(100),
        nullable=False
    )
    url = db.Column(
        db.String(255),
        nullable=False
    )

# ==========================================================
# Categorias
# ==========================================================
class CategoryLink(db.Model):
    __tablename__ = "category_links"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )

    label = db.Column(
        db.String(100),
        nullable=False
    )

    url = db.Column(
        db.String(255),
        nullable=False
    )

# ==========================================================
# Contato
# ==========================================================
class ContactInfo(db.Model):
    __tablename__ = "contact_info"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    address = db.Column(db.Text)
    phone = db.Column(db.String(30))
    email = db.Column(db.String(150))
    hours = db.Column(db.String(100))

# ==========================================================
# Links legais
# ==========================================================
class LegalLink(db.Model):
    __tablename__ = "legal_links"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )

    label = db.Column(
        db.String(150),
        nullable=False
    )

    url = db.Column(
        db.String(255),
        nullable=False
    )

# ==========================================================
# Usuários
# ==========================================================
class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    name = db.Column(
        db.String(150),
        nullable=False
    )

    email = db.Column(
        db.String(150),
        unique=True,
        nullable=False
    )

    password = db.Column(
        db.String(255),
        nullable=False
    )

    phone = db.Column(db.String(30))

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    carts = db.relationship(
        "Cart",
        backref="user",
        lazy=True,
        cascade="all, delete-orphan"
    )
    is_admin = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )


    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password, password)

    def get_id(self):
        return str(self.id)

    def __repr__(self):
        return f"<User {self.email}>"

# ==========================================================
# Carrinhos
# ==========================================================
class Cart(db.Model):
    __tablename__ = "carts"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )

    user_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("users.id"),
        nullable=False
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    items = db.relationship(
        "CartItem",
        backref="cart",
        lazy=True,
        cascade="all, delete-orphan"
    )


# ==========================================================
# Itens do Carrinho
# ==========================================================
class CartItem(db.Model):
    __tablename__ = "cart_items"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )

    cart_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("carts.id"),
        nullable=False
    )

    product_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("products.id"),
        nullable=False
    )

    quantity = db.Column(
        db.Integer,
        default=1
    )

    unit_price = db.Column(
        db.Numeric(10, 2),
        nullable=False
    )

    product = db.relationship(
        "Product",
        lazy=True
    )
