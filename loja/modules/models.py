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
    banner_cta_url = db.Column(db.String(255))  # NOVO: link do CTA do banner (antes só ia pra /produtos)
    promo_badge = db.Column(db.String(100))
    copyright_year = db.Column(db.Integer)
    copyright_text = db.Column(db.String(255))

    # NOVOS: usados na seção "Selos de confiança" da home (antes eram valores fixos no HTML)
    exchange_days = db.Column(db.Integer, default=30)
    max_installments = db.Column(db.Integer, default=12)
    free_shipping_min = db.Column(db.Numeric(10, 2), default=249)


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
    link = db.Column(db.String(255))          # NOVO: destino do botão "Ver coleção"

    is_active = db.Column(db.Boolean, default=True)   # NOVO: liga/desliga sem apagar
    order = db.Column(db.Integer, default=0)          # NOVO: ordem de exibição no carrossel
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<HeroSlide {self.title}>"


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

    favorited_by = db.relationship(   # NOVO: para a lista "meus favoritos"
        "Favorite",
        backref="product",
        lazy=True,
        cascade="all, delete-orphan"
    )

    # Itens de carrinho que apontam para este produto. O produto continua sendo
    # a fonte de verdade para nome, descrição e imagens; o CartItem guarda apenas
    # a seleção feita pelo cliente e o preço aplicado naquele momento.
    cart_items = db.relationship(
        "CartItem",
        back_populates="product",
        lazy=True,
        passive_deletes=True
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

    # NOVO: liga a promoção a um produto real do catálogo. Sem isso o botão
    # "Aproveitar" da home não tinha para onde ir (Promotion.id != Product.id).
    product_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("products.id"),
        nullable=True
    )
    product = db.relationship("Product", lazy=True)

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
    starts_at = db.Column(db.DateTime)            # NOVO
    ends_at = db.Column(db.DateTime)               # NOVO: alimenta o cronômetro da home
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)  # NOVO: para ordenar "mais recentes"


# ==========================================================
# Newsletter
# ==========================================================
class NewsletterSubscriber(db.Model):   # NOVO: faltava para o form de newsletter funcionar
    __tablename__ = "newsletter_subscribers"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    email = db.Column(db.String(150), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<NewsletterSubscriber {self.email}>"


# ==========================================================
# Favoritos
# ==========================================================
class Favorite(db.Model):   # NOVO: favoritos ligados ao usuário (persistem entre dispositivos)
    __tablename__ = "favorites"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    user_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False
    )
    product_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("user_id", "product_id", name="uq_favorite_user_product"),
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

    slug = db.Column(
        db.Text,
        nullable=False
    )

    image = db.Column(
        db.Text,
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
# Endereços
# ==========================================================

class Address(db.Model):
    __tablename__ = "addresses"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )

    user_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False
    )

    recipient_name = db.Column(
        db.String(150),
        nullable=False
    )

    phone = db.Column(
        db.String(30)
    )

    zip_code = db.Column(
        db.String(10),
        nullable=False
    )

    street = db.Column(
        db.String(255),
        nullable=False
    )

    number = db.Column(
        db.String(20),
        nullable=False
    )

    complement = db.Column(
        db.String(255)
    )

    neighborhood = db.Column(
        db.String(150),
        nullable=False
    )

    city = db.Column(
        db.String(150),
        nullable=False
    )

    state = db.Column(
        db.String(2),
        nullable=False
    )

    is_default = db.Column(
        db.Boolean,
        default=False,
        nullable=False
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
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
        back_populates="user",
        lazy=True,
        cascade="all, delete-orphan",
        passive_deletes=True
    )

    addresses = db.relationship(
        "Address",
        backref="user",
        lazy=True,
        cascade="all, delete-orphan"
    )

    favorites = db.relationship(   # NOVO: acesso via user.favorites
        "Favorite",
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
        # CORRIGIDO: a coluna se chama `password`, não `password_hash`.
        # Como estava, set_password() criava um atributo novo em memória
        # (self.password_hash) que nunca era salvo no banco, e a senha
        # real (self.password) nunca era hasheada.
        self.password = generate_password_hash(password)

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
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )

    user = db.relationship(
        "User",
        back_populates="carts"
    )

    items = db.relationship(
        "CartItem",
        back_populates="cart",
        lazy=True,
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="CartItem.created_at"
    )

    @property
    def total_items(self):
        """Quantidade total de unidades presentes no carrinho."""
        return sum(item.quantity for item in self.items)

    @property
    def subtotal(self):
        """Soma dos preços registrados nos itens do carrinho."""
        return sum((item.line_total for item in self.items), 0)

    def __repr__(self):
        return f"<Cart {self.id} user={self.user_id}>"


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
        db.ForeignKey("carts.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    product_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # Tamanho escolhido na página do produto. O nome selected_size foi mantido
    # explícito para não confundir com dimensões físicas ou estoque do produto.
    selected_size = db.Column(
        db.String(10),
        nullable=False
    )

    quantity = db.Column(
        db.Integer,
        default=1,
        nullable=False
    )

    # Snapshot do preço no momento em que o item entra no carrinho. Assim uma
    # alteração posterior em Product.price não muda silenciosamente o carrinho.
    unit_price = db.Column(
        db.Numeric(10, 2),
        nullable=False
    )

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False
    )

    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )

    cart = db.relationship(
        "Cart",
        back_populates="items"
    )

    product = db.relationship(
        "Product",
        back_populates="cart_items",
        lazy="joined"
    )

    __table_args__ = (
        # O mesmo produto pode aparecer em tamanhos diferentes, mas não deve
        # criar duas linhas para o mesmo produto e o mesmo tamanho no carrinho.
        db.UniqueConstraint(
            "cart_id",
            "product_id",
            "selected_size",
            name="uq_cart_item_product_size"
        ),
        db.CheckConstraint(
            "quantity >= 1",
            name="ck_cart_item_quantity_positive"
        ),
        db.CheckConstraint(
            "unit_price >= 0",
            name="ck_cart_item_unit_price_nonnegative"
        ),
    )

    @property
    def line_total(self):
        """Preço total deste item, sem frete."""
        return self.unit_price * self.quantity

    # Compatibilidade opcional com templates antigos que usem item.size.
    @property
    def size(self):
        return self.selected_size

    @size.setter
    def size(self, value):
        self.selected_size = value

    def __repr__(self):
        return (
            f"<CartItem {self.id} product={self.product_id} "
            f"size={self.selected_size} quantity={self.quantity}>"
        )