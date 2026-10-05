import uuid
from decimal import Decimal
from loja import db
from loja.utils import utcnow
from flask_login import UserMixin
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import validates
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
    exchange_days = db.Column(db.Integer, default=7)
    max_installments = db.Column(db.Integer, default=12)
    # Sem promessa de frete grátis: sem default (linhas antigas podem conter
    # um limite legado, mas nenhum código mais lê este campo para precificar
    # ou anunciar).
    free_shipping_min = db.Column(db.Numeric(10, 2), default=None)


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
    created_at = db.Column(db.DateTime, default=utcnow)

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
        default=utcnow
    )

    # Campos genéricos de canal de fornecedor (dropshipping).
    channel_type = db.Column(
        db.String(20),
        nullable=False,
        default="manual",
        server_default="manual",
    )
    api_key = db.Column(db.Text)
    api_secret = db.Column(db.Text)
    access_token = db.Column(db.Text)
    access_token_expires_at = db.Column(db.DateTime)
    seller_id = db.Column(db.String(64))
    default_currency = db.Column(
        db.String(3),
        nullable=False,
        default="BRL",
        server_default="BRL",
    )
    is_sync_enabled = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=db.false(),
    )
    last_sync_at = db.Column(db.DateTime)
    sync_status = db.Column(db.String(20))
    sync_error = db.Column(db.Text)

    products = db.relationship(
        "Product",
        secondary=product_supplier,
        back_populates="suppliers",
        lazy=True,
    )
    supplier_products = db.relationship(
        "SupplierProduct",
        back_populates="supplier",
        lazy=True,
        cascade="all, delete-orphan",
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

    name = db.Column(
        db.Text,
        nullable=False
    )

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
        default=utcnow
    )

    # Campos de integração/dropshipping (aditivos; novos, com defaults).
    external_id = db.Column(db.String(64), index=True)
    source = db.Column(
        db.String(20),
        nullable=False,
        default="internal",
        server_default="internal",
    )
    is_draft = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=db.false(),
    )
    cost = db.Column(db.Numeric(10, 2))
    wholesale_price = db.Column(db.Numeric(10, 2))
    category_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sync_status = db.Column(db.String(20))
    last_sync_at = db.Column(db.DateTime)
    sync_error = db.Column(db.Text)
    # Precificação dropshipping (Fase 6). Moeda de VENDA: no import AliExpress
    # preenche a moeda resolvida; produtos internos/legados permanecem BRL.
    currency = db.Column(
        db.String(3),
        nullable=False,
        default="BRL",
        server_default="BRL",
    )
    # Margem alvo EXPLÍCITA. Sem margem padrão: NULL = não precificável nem
    # publicável. Regra de negócio (0, 100) reforçada no banco.
    target_margin_percent = db.Column(db.Numeric(5, 2))
    # Estados: unpriced | blocked | ready | priced | stale.
    pricing_status = db.Column(
        db.String(20),
        nullable=False,
        default="unpriced",
        server_default="unpriced",
    )
    pricing_error = db.Column(db.Text)
    pricing_updated_at = db.Column(db.DateTime)

    # Defesa de banco: AliExpress só pode ficar ATIVO se NÃO for rascunho E
    # estiver priced. O resto da validação (landed cost, moeda, margem alvo,
    # variantes) é responsabilidade de publication.evaluate_publication().
    __table_args__ = (
        db.CheckConstraint(
            "target_margin_percent IS NULL "
            "OR (target_margin_percent > 0 AND target_margin_percent < 100)",
            name="ck_product_target_margin_range",
        ),
        db.CheckConstraint(
            "source != 'aliexpress' "
            "OR is_active = false "
            "OR (is_draft = false AND pricing_status = 'priced')",
            name="ck_product_aliexpress_publication_guard",
        ),
        # Estados canônicos de pricing (unpriced | blocked | ready | priced |
        # stale). Reforça o contrato no banco.
        db.CheckConstraint(
            "pricing_status IN "
            "('unpriced', 'blocked', 'ready', 'priced', 'stale')",
            name="ck_product_pricing_status",
        ),
        # List público da home + recorte de categorias (PR #103/TTFB):
        # filtro is_active/is_draft + ordenação por created_at (a janela por
        # category ordena o mesmo par). Instalações existentes aplicam via
        # migration 0033 (create_all não altera tabelas já existentes).
        db.Index(
            "ix_products_public_recent",
            "is_active",
            "is_draft",
            "created_at",
        ),
    )

    suppliers = db.relationship(
        "Supplier",
        secondary=product_supplier,
        back_populates="products",
        lazy=True
    )

    favorited_by = db.relationship(
        "Favorite",
        backref="product",
        lazy=True,
        cascade="all, delete-orphan"
    )

    cart_items = db.relationship(
        "CartItem",
        back_populates="product",
        lazy=True,
        passive_deletes=True
    )

    # Promoções vinculadas ao produto
    promotions = db.relationship(
        "Promotion",
        back_populates="product",
        lazy=True,
        cascade="all, delete-orphan",
        passive_deletes=True
    )

    category_obj = db.relationship(
        "Category",
        back_populates="products",
        lazy=True,
    )
    images = db.relationship(
        "ProductImage",
        back_populates="product",
        lazy=True,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    variants = db.relationship(
        "ProductVariant",
        back_populates="product",
        lazy=True,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    supplier_links = db.relationship(
        "SupplierProduct",
        back_populates="product",
        lazy=True,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self):
        return f"<Product {self.name}>"

# ==========================================================
# Aquisição agregada (FASE 9.0.0-G)
# ==========================================================
class AcquisitionDailyMetric(db.Model):
    __tablename__ = "acquisition_daily_metrics"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    day = db.Column(db.Date, nullable=False)
    channel = db.Column(db.String(140), nullable=False)
    sessions = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        db.UniqueConstraint("day", "channel", name="uq_acquisition_daily_day_channel"),
        db.CheckConstraint("sessions >= 0", name="ck_acquisition_daily_sessions_nonnegative"),
    )


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

    # Produto que está em promoção
    product_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey(
            "products.id",
            ondelete="CASCADE"
        ),
        nullable=False,
        index=True
    )

    product = db.relationship(
        "Product",
        back_populates="promotions",
        lazy="joined"
    )

    # Único preço armazenado pela promoção.
    # O preço original vem de Product.price.
    sale_price = db.Column(
        db.Numeric(10, 2),
        nullable=False
    )

    is_active = db.Column(
        db.Boolean,
        nullable=False,
        default=True
    )

    starts_at = db.Column(
        db.DateTime
    )

    ends_at = db.Column(
        db.DateTime
    )

    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utcnow
    )

    __table_args__ = (
        db.CheckConstraint(
            "sale_price >= 0",
            name="ck_promotion_sale_price_nonnegative"
        ),
        db.CheckConstraint(
            "starts_at IS NULL OR ends_at IS NULL OR ends_at > starts_at",
            name="ck_promotion_valid_period"
        ),
    )

    # ==========================================
    # Dados obtidos automaticamente de Product
    # ==========================================

    @property
    def name(self):
        return self.product.name if self.product else None

    @property
    def description(self):
        return self.product.description if self.product else None

    @property
    def image(self):
        return self.product.image1 if self.product else None

    @property
    def image1(self):
        return self.product.image1 if self.product else None

    @property
    def image2(self):
        return self.product.image2 if self.product else None

    @property
    def image3(self):
        return self.product.image3 if self.product else None

    @property
    def image4(self):
        return self.product.image4 if self.product else None

    @property
    def original_price(self):
        return self.product.price if self.product else None

    @property
    def category(self):
        return self.product.category if self.product else None

    def __repr__(self):
        return (
            f"<Promotion product={self.product_id} "
            f"sale_price={self.sale_price}>"
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
    created_at = db.Column(db.DateTime, default=utcnow)  # NOVO: para ordenar "mais recentes"


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
    created_at = db.Column(db.DateTime, default=utcnow)

    def __repr__(self):
        return f"<NewsletterSubscriber {self.email}>"


# ==========================================================
# Cupons de desconto
# ==========================================================
class Coupon(db.Model):
    __tablename__ = "coupons"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )

    # O código é normalizado para maiúsculas pelo validator abaixo.
    # O índice funcional em UPPER(code) também impede duplicações com
    # diferenças apenas entre letras maiúsculas e minúsculas.
    code = db.Column(
        db.String(50),
        nullable=False
    )

    description = db.Column(
        db.String(255)
    )

    # Tipos aceitos:
    # - percentage: percentual sobre o subtotal
    # - fixed: valor fixo em reais
    discount_type = db.Column(
        db.String(20),
        nullable=False
    )

    discount_value = db.Column(
        db.Numeric(10, 2),
        nullable=False
    )

    minimum_order_amount = db.Column(
        db.Numeric(10, 2),
        nullable=False,
        default=0
    )

    # Limita o desconto calculado em cupons percentuais.
    # Exemplo: 20% de desconto com teto máximo de R$ 50,00.
    maximum_discount_amount = db.Column(
        db.Numeric(10, 2)
    )

    starts_at = db.Column(
        db.DateTime
    )

    ends_at = db.Column(
        db.DateTime
    )

    # Limite total de utilizações do cupom. NULL significa ilimitado.
    usage_limit = db.Column(
        db.Integer
    )

    # Deve ser incrementado somente depois que o pedido for confirmado.
    times_used = db.Column(
        db.Integer,
        nullable=False,
        default=0
    )

    # Permite marcar cupons exclusivos para a primeira compra.
    first_purchase_only = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )

    is_active = db.Column(
        db.Boolean,
        nullable=False,
        default=True
    )

    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utcnow
    )

    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow
    )

    __table_args__ = (
        db.Index(
            "uq_coupons_code_upper",
            db.func.upper(code),
            unique=True
        ),
        db.Index(
            "ix_coupons_active_period",
            "is_active",
            "starts_at",
            "ends_at"
        ),
        db.CheckConstraint(
            "discount_type IN ('percentage', 'fixed')",
            name="ck_coupon_discount_type"
        ),
        db.CheckConstraint(
            "discount_value > 0",
            name="ck_coupon_discount_value_positive"
        ),
        db.CheckConstraint(
            "discount_type <> 'percentage' OR discount_value <= 100",
            name="ck_coupon_percentage_max_100"
        ),
        db.CheckConstraint(
            "minimum_order_amount >= 0",
            name="ck_coupon_minimum_order_nonnegative"
        ),
        db.CheckConstraint(
            "maximum_discount_amount IS NULL "
            "OR maximum_discount_amount >= 0",
            name="ck_coupon_maximum_discount_nonnegative"
        ),
        db.CheckConstraint(
            "usage_limit IS NULL OR usage_limit >= 1",
            name="ck_coupon_usage_limit_positive"
        ),
        db.CheckConstraint(
            "times_used >= 0",
            name="ck_coupon_times_used_nonnegative"
        ),
        db.CheckConstraint(
            "usage_limit IS NULL OR times_used <= usage_limit",
            name="ck_coupon_times_used_within_limit"
        ),
        db.CheckConstraint(
            "starts_at IS NULL OR ends_at IS NULL OR ends_at > starts_at",
            name="ck_coupon_valid_period"
        ),
    )

    @validates("code")
    def validate_code(self, key, value):
        normalized_code = (value or "").strip().upper()

        if not normalized_code:
            raise ValueError("O código do cupom é obrigatório.")

        if len(normalized_code) > 50:
            raise ValueError(
                "O código do cupom deve possuir no máximo 50 caracteres."
            )

        return normalized_code

    @property
    def remaining_uses(self):
        """Retorna quantos usos ainda restam ou None quando for ilimitado."""
        if self.usage_limit is None:
            return None

        return max(self.usage_limit - self.times_used, 0)

    def is_available(self, subtotal=0, now=None):
        """
        Verifica se o cupom está ativo, dentro da validade, com usos
        disponíveis e se o pedido atingiu o valor mínimo.
        """
        current_time = now or utcnow()
        order_subtotal = Decimal(str(subtotal or 0))

        if not self.is_active:
            return False

        if self.starts_at and current_time < self.starts_at:
            return False

        if self.ends_at and current_time >= self.ends_at:
            return False

        if self.usage_limit is not None and self.times_used >= self.usage_limit:
            return False

        if order_subtotal < Decimal(str(self.minimum_order_amount or 0)):
            return False

        return True

    def calculate_discount(self, subtotal):
        """
        Calcula o desconto sem permitir que o valor ultrapasse o subtotal.
        Retorna Decimal('0.00') quando o cupom não puder ser utilizado.
        """
        order_subtotal = Decimal(str(subtotal or 0))

        if order_subtotal <= 0 or not self.is_available(order_subtotal):
            return Decimal("0.00")

        discount_value = Decimal(str(self.discount_value))

        if self.discount_type == "percentage":
            discount = (
                order_subtotal * discount_value / Decimal("100")
            )

            if self.maximum_discount_amount is not None:
                discount = min(
                    discount,
                    Decimal(str(self.maximum_discount_amount))
                )
        else:
            discount = discount_value

        return min(discount, order_subtotal).quantize(Decimal("0.01"))

    def __repr__(self):
        return (
            f"<Coupon {self.code} type={self.discount_type} "
            f"value={self.discount_value}>"
        )


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
    created_at = db.Column(db.DateTime, default=utcnow)

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
        default=utcnow
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
        default=utcnow
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

    # Versão de autenticação (reset de senha, 0027). Embutida no payload de
    # sessão/lembrar-me via get_id(); trocar a senha incrementa o valor e
    # invalida todos os logins antigos, inclusive "manter conectado".
    auth_version = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default="1"
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
        # Payload versionado: "uuid.versao". O user_loader valida a versão —
        # sem ponto (payload legado) equivale à versão 1.
        return f"{self.id}.{self.auth_version}"

    def __repr__(self):
        return f"<User {self.email}>"


# ==========================================================
# Recuperação de senha (esqueci minha senha — 0027)
# ==========================================================
class PasswordResetToken(db.Model):
    """Token de redefinição de senha — guarda somente o hash sha256."""

    __tablename__ = "password_reset_tokens"

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

    token_hash = db.Column(
        db.String(64),
        nullable=False,
        unique=True
    )

    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utcnow
    )

    expires_at = db.Column(
        db.DateTime,
        nullable=False
    )

    used_at = db.Column(
        db.DateTime,
        nullable=True
    )

    user = db.relationship("User")

    __table_args__ = (
        db.CheckConstraint(
            "expires_at > created_at",
            name="ck_password_reset_tokens_window"
        ),
        # No máximo 1 token não consumido por usuário.
        db.Index(
            "uq_password_reset_active_user",
            "user_id",
            unique=True,
            sqlite_where=db.text("used_at IS NULL"),
            postgresql_where=db.text("used_at IS NULL"),
        ),
    )


class AuthRateLimit(db.Model):
    """Janela fixa de tentativas por escopo (ex.: por IP ou por e-mail)."""

    __tablename__ = "auth_rate_limits"

    scope = db.Column(
        db.String(32),
        primary_key=True
    )

    key_hash = db.Column(
        db.String(64),
        primary_key=True
    )

    window_started_at = db.Column(
        db.DateTime,
        nullable=False
    )

    attempts = db.Column(
        db.Integer,
        nullable=False
    )

    __table_args__ = (
        db.CheckConstraint(
            "attempts >= 0",
            name="ck_auth_rate_limits_attempts"
        ),
    )


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
        default=utcnow,
        nullable=False
    )

    updated_at = db.Column(
        db.DateTime,
        default=utcnow,
        onupdate=utcnow,
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
    # VARCHAR(40) pela migração 0028: rótulos completos de variante (ex.:
    # "11-12T(Size 160)"); nunca truncar em Python — falhar explícito acima.
    selected_size = db.Column(
        db.String(40),
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
        default=utcnow,
        nullable=False
    )

    updated_at = db.Column(
        db.DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False
    )

    variant_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("product_variants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    cart = db.relationship(
        "Cart",
        back_populates="items"
    )
    variant = db.relationship(
        "ProductVariant",
        back_populates="cart_items",
    )

    product = db.relationship(
        "Product",
        back_populates="cart_items",
        lazy="joined"
    )

    __table_args__ = (
        # Itens com variante (AliExpress) são únicos por (cart, variant):
        # permite Rosa/M e Azul/M coexistirem no mesmo carrinho.
        db.Index(
            "uq_cart_item_variant",
            "cart_id",
            "variant_id",
            unique=True,
            sqlite_where=db.text("variant_id IS NOT NULL"),
            postgresql_where=db.text("variant_id IS NOT NULL"),
        ),
        # Itens legados (sem variante) continuam únicos por
        # (cart, product, selected_size).
        db.Index(
            "uq_cart_item_product_size",
            "cart_id",
            "product_id",
            "selected_size",
            unique=True,
            sqlite_where=db.text("variant_id IS NULL"),
            postgresql_where=db.text("variant_id IS NULL"),
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


class Shipment(db.Model):
    __tablename__ = "shipments"

    id = db.Column(
        db.BigInteger,
        primary_key=True,
    )

    order_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    tracking_code = db.Column(
        db.Text,
        unique=True,
        nullable=False,
        index=True,
    )

    carrier = db.Column(
        db.Text,
        nullable=True,
    )

    provider = db.Column(
        db.Text,
        nullable=False,
        default="manual",
    )

    status = db.Column(
        db.Text,
        nullable=False,
        default="aguardando_postagem",
    )

    estimated_delivery = db.Column(
        db.Date,
        nullable=True,
    )

    last_checked_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
    )

    # "metadata" é reservado pelo SQLAlchemy Declarative.
    shipment_metadata = db.Column(
        "metadata",
        JSONB,
        nullable=True,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )

    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    # Campos de rastreio de fornecedor (dropshipping).
    ali_order_id = db.Column(db.String(64), index=True)
    external_shipment_id = db.Column(db.String(64))

    order = db.relationship(
        "Order",
        back_populates="shipments",
    )

    events = db.relationship(
        "TrackingEvent",
        back_populates="shipment",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: TrackingEvent.event_at.desc(),
    )

    def __repr__(self):
        return (
            f"<Shipment {self.id} order={self.order_id} "
            f"tracking={self.tracking_code}>"
        )


class TrackingEvent(db.Model):
    __tablename__ = "tracking_events"

    id = db.Column(
        db.BigInteger,
        primary_key=True,
    )

    shipment_id = db.Column(
        db.BigInteger,
        db.ForeignKey("shipments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    provider_event_id = db.Column(
        db.Text,
        nullable=True,
    )

    status = db.Column(
        db.Text,
        nullable=False,
    )

    description = db.Column(
        db.Text,
        nullable=False,
    )

    location = db.Column(
        db.Text,
        nullable=True,
    )

    event_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )

    raw_payload = db.Column(
        JSONB,
        nullable=True,
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )

    shipment = db.relationship(
        "Shipment",
        back_populates="events",
    )

    def __repr__(self):
        return (
            f"<TrackingEvent {self.id} shipment={self.shipment_id} "
            f"status={self.status}>"
        )


# Este import deve permanecer no final do arquivo, depois de Shipment.
# Assim todos os nomes usados nos relacionamentos já estão registrados.
from loja.modules.getway_mercado_pago.payment_models import (  # noqa: E402,F401
    Order,
    OrderItem,
    Payment,
)


# ==========================================================
# Categorias normalizadas (dropshipping / mapeamento de fornecedor)
# ==========================================================
class Category(db.Model):
    __tablename__ = "categories"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name = db.Column(db.String(100), nullable=False)
    slug = db.Column(db.String(120), nullable=False, unique=True)
    parent_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("categories.id", ondelete="CASCADE"),
        nullable=True,
    )
    source = db.Column(
        db.String(20),
        nullable=False,
        default="internal",
        server_default="internal",
    )
    external_id = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    parent = db.relationship(
        "Category",
        remote_side=[id],
        backref=db.backref("children", lazy=True),
        lazy=True,
    )
    products = db.relationship(
        "Product",
        back_populates="category_obj",
        lazy=True,
    )


# ==========================================================
# Imagens de produto (galeria, mais de 4)
# ==========================================================
class ProductImage(db.Model):
    __tablename__ = "product_images"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    product_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    url = db.Column(db.Text, nullable=False)
    alt = db.Column(db.Text)
    position = db.Column(db.Integer, nullable=False, default=0)
    is_primary = db.Column(db.Boolean, nullable=False, default=False)

    product = db.relationship("Product", back_populates="images")


# ==========================================================
# Variantes / SKUs de produto (genérico)
# ==========================================================
class ProductVariant(db.Model):
    __tablename__ = "product_variants"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    product_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sku = db.Column(db.String(64))
    external_sku_id = db.Column(db.String(64), index=True)
    supplier_product_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("supplier_products.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    supplier_sku_id = db.Column(db.String(64))
    color = db.Column(db.String(60))
    # VARCHAR(40) pela migração 0028 (alinhado à política VARIANT_LABEL_MAX_LEN;
    # nunca truncar em Python — excessos rejeitados explicitamente na importação).
    size = db.Column(db.String(40))
    attributes = db.Column(JSONB)
    stock = db.Column(db.Integer, nullable=False, default=0)
    price = db.Column(db.Numeric(10, 2))
    sale_price = db.Column(db.Numeric(10, 2))
    image_url = db.Column(db.Text)
    weight_kg = db.Column(db.Numeric(8, 3))
    dimensions = db.Column(db.Text)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    # Seleção de operação nacional (Etapa 1): somente variante marcada e ativa
    # participa do gate de origem nacional. FALSE = catálogo internacional
    # comum, que NUNCA bloqueia por este recurso.
    national_shipping_operation = db.Column(
        db.Boolean, nullable=False, default=False, server_default=db.false()
    )
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        db.UniqueConstraint(
            "supplier_product_id", "supplier_sku_id",
            name="uq_product_variant_supplier",
        ),
        db.CheckConstraint("stock >= 0", name="ck_product_variant_stock_nonneg"),
        db.CheckConstraint("price >= 0", name="ck_product_variant_price_nonneg"),
        db.CheckConstraint(
            "sale_price >= 0", name="ck_product_variant_sale_price_nonneg"
        ),
    )

    product = db.relationship("Product", back_populates="variants")
    cart_items = db.relationship("CartItem", back_populates="variant")
    order_items = db.relationship("OrderItem", back_populates="variant")


# ==========================================================
# Produto de fornecedor (vínculo enriquecido; substitui product_supplier)
# ==========================================================
class SupplierProduct(db.Model):
    __tablename__ = "supplier_products"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    supplier_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("suppliers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    supplier_product_id = db.Column(db.String(64), index=True)
    external_sku_id = db.Column(db.String(64))
    cost = db.Column(db.Numeric(10, 2))
    wholesale_price = db.Column(db.Numeric(10, 2))
    currency = db.Column(
        db.String(3),
        nullable=False,
        default="BRL",
        server_default="BRL",
    )
    # Estado do frete do fornecedor (Fase 4/5). Ausência de frete NUNCA vira
    # zero: provider_unavailable mantém shipping_cost/landed_cost como NULL.
    freight_status = db.Column(
        db.String(30),
        nullable=False,
        default="unknown",
        server_default="unknown",
    )
    shipping_cost = db.Column(db.Numeric(10, 2))
    landed_cost = db.Column(db.Numeric(10, 2))
    # NULL means unverified; zero must be explicitly evidenced.
    import_duty = db.Column(db.Numeric(10, 2), nullable=True)
    icms = db.Column(db.Numeric(10, 2), nullable=True)
    freight_currency = db.Column(db.String(3))
    sync_status = db.Column(db.String(20))
    last_sync_at = db.Column(db.DateTime)
    sync_error = db.Column(db.Text)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(
        db.DateTime,
        default=utcnow,
        onupdate=utcnow,
    )

    supplier = db.relationship("Supplier", back_populates="supplier_products")
    product = db.relationship("Product", back_populates="supplier_links")

    __table_args__ = (
        db.UniqueConstraint(
            "supplier_id", "supplier_product_id",
            name="uq_supplier_product_external",
        ),
        db.CheckConstraint("cost >= 0", name="ck_supplier_product_cost_nonneg"),
        db.CheckConstraint(
            "(import_duty IS NULL AND icms IS NULL) OR "
            "(import_duty IS NOT NULL AND icms IS NOT NULL AND "
            "import_duty BETWEEN 0 AND 99999999.99 AND icms BETWEEN 0 AND 99999999.99)",
            name="ck_supplier_product_purchase_taxes",
        ),
        db.CheckConstraint(
            "wholesale_price >= 0",
            name="ck_supplier_product_wholesale_nonneg",
        ),
        db.CheckConstraint(
            "shipping_cost IS NULL OR shipping_cost >= 0",
            name="ck_supplier_product_shipping_cost_nonneg",
        ),
        db.CheckConstraint(
            "landed_cost IS NULL OR landed_cost >= 0",
            name="ck_supplier_product_landed_cost_nonneg",
        ),
    )


# ==========================================================
# Origem de envio com evidência (integração AliExpress, Etapa 1)
# ==========================================================
class SupplierSkuShippingOrigin(db.Model):
    """Classificação de origem de envio por SKU ou por cobertura do vínculo.

    Duas escritas independentes e nunca misturadas:
      * ``manual_origin``: declaração do admin, SEMPRE com ``evidence_ref`` e
        ``confirmed_at`` (CHECK no banco). É a fonte que um humano assume.
      * ``api_origin``: valor observado em dado do fornecedor, com
        ``api_observed_at``. Nunca sobrescreve a confirmação manual.
    Conflito manual × api é sinalizado na resolução, nunca silenciado.

    Escopo XOR por linha (``supplier_sku_id`` OU ``covers_all_variants``);
    a resolução dá precedência ao SKU sobre a cobertura. Ausência de linha =
    origem desconhecida (fail-closed); nada é presumido como nacional.
    """

    __tablename__ = "supplier_sku_shipping_origins"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    supplier_product_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("supplier_products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    supplier_sku_id = db.Column(db.String(64))
    covers_all_variants = db.Column(
        db.Boolean, nullable=False, default=False, server_default=db.false()
    )
    api_origin = db.Column(db.String(16))
    manual_origin = db.Column(db.String(16))
    origin_country = db.Column(db.String(2))
    evidence_ref = db.Column(db.Text)
    confirmed_by_user_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    confirmed_at = db.Column(db.DateTime)
    api_observed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(
        db.DateTime, default=utcnow, onupdate=utcnow
    )

    supplier_product = db.relationship("SupplierProduct", lazy=True)

    __table_args__ = (
        # Índices únicos parciais (mesmos nomes da migration 0034): uma linha
        # por SKU e no máximo uma linha de cobertura total por vínculo.
        db.Index(
            "uq_shipping_origin_variant_sku",
            "supplier_product_id", "supplier_sku_id",
            unique=True,
            sqlite_where=db.text("supplier_sku_id IS NOT NULL"),
            postgresql_where=db.text("supplier_sku_id IS NOT NULL"),
        ),
        db.Index(
            "uq_shipping_origin_cover_all",
            "supplier_product_id",
            unique=True,
            sqlite_where=db.text("covers_all_variants"),
            postgresql_where=db.text("covers_all_variants"),
        ),
        db.CheckConstraint(
            "(supplier_sku_id IS NOT NULL AND covers_all_variants = FALSE) "
            "OR (supplier_sku_id IS NULL AND covers_all_variants = TRUE)",
            name="ck_shipping_origin_scope",
        ),
        db.CheckConstraint(
            "api_origin IS NULL OR api_origin IN ('brazil', 'foreign')",
            name="ck_shipping_origin_api_origin",
        ),
        db.CheckConstraint(
            "manual_origin IS NULL OR manual_origin IN ('brazil', 'foreign')",
            name="ck_shipping_origin_manual_origin",
        ),
        db.CheckConstraint(
            "manual_origin IS NULL OR "
            "(evidence_ref IS NOT NULL AND confirmed_at IS NOT NULL)",
            name="ck_shipping_origin_manual_evidence",
        ),
        db.CheckConstraint(
            "api_origin IS NULL OR api_observed_at IS NOT NULL",
            name="ck_shipping_origin_api_evidence",
        ),
        db.CheckConstraint(
            "origin_country IS NULL OR length(origin_country) = 2",
            name="ck_shipping_origin_country",
        ),
    )


# ==========================================================
# Pedido de fornecedor (dropshipping) — separado de Order
# ==========================================================
class AliExpressOrder(db.Model):
    __tablename__ = "aliexpress_orders"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    order_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("orders.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    supplier_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("suppliers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    ali_order_id = db.Column(db.String(64), unique=True)
    state = db.Column(
        db.String(30),
        nullable=False,
        default="queued",
        server_default="queued",
    )
    idempotency_key = db.Column(db.String(64), unique=True)
    attempt_key = db.Column(db.String(64), nullable=True, index=True)
    ali_status = db.Column(db.Text)
    raw_payload = db.Column(JSONB)
    last_error = db.Column(db.Text)
    attempt_count = db.Column(db.Integer, nullable=False, default=0)
    next_attempt_at = db.Column(db.DateTime)
    queued_at = db.Column(db.DateTime, default=utcnow)
    ready_at = db.Column(db.DateTime)
    submitted_at = db.Column(db.DateTime)
    confirmed_at = db.Column(db.DateTime)
    shipped_at = db.Column(db.DateTime)
    delivered_at = db.Column(db.DateTime)
    cancelled_at = db.Column(db.DateTime)
    last_attempt_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(
        db.DateTime,
        default=utcnow,
        onupdate=utcnow,
    )

    order = db.relationship("Order", backref="aliexpress_orders")
    supplier = db.relationship("Supplier")

    __table_args__ = (
        db.CheckConstraint(
            "state IN "
            "('queued', 'blocked', 'ready', 'submitting', 'submitted', "
            "'confirmed', 'shipped', 'delivered', 'cancel_requested', "
            "'cancelled', 'failed', 'manual_review', 'pending', "
            "'pending_reconciliation')",
            name="ck_aliexpress_order_state",
        ),
    )


# ==========================================================
# Tentativa de submissão AliExpress (barreira durável 8.0.11B)
# ==========================================================
class AliExpressSubmissionAttempt(db.Model):
    __tablename__ = "aliexpress_submission_attempts"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    aliexpress_order_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("aliexpress_orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_key = db.Column(
        db.String(64),
        nullable=False,
        unique=True,
    )
    state = db.Column(
        db.String(20),
        nullable=False,
    )
    started_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utcnow,
    )
    completed_at = db.Column(
        db.DateTime,
        nullable=True,
    )
    last_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utcnow,
    )
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    __table_args__ = (
        db.CheckConstraint(
            "state IN "
            "('in_flight','confirmed','rejected','unknown')",
            name="ck_attempt_state",
        ),
    )

    aliexpress_order = db.relationship(
        "AliExpressOrder",
        backref=db.backref("submission_attempts", cascade="all, delete-orphan"),
    )

    def __repr__(self):
        return (
            f"<AliExpressSubmissionAttempt {self.id} "
            f"order={self.aliexpress_order_id} "
            f"state={self.state}>"
        )


# ==========================================================
# Pedido remoto AliExpress (cardinalidade 0/1/N)
# ==========================================================
class AliExpressRemoteOrder(db.Model):
    __tablename__ = "aliexpress_remote_orders"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    aliexpress_order_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("aliexpress_orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    remote_order_id = db.Column(
        db.String(64),
        nullable=False,
    )
    position = db.Column(
        db.Integer,
        nullable=False,
    )
    attempt_key = db.Column(
        db.String(64),
        nullable=True,
    )
    remote_status = db.Column(db.Text, nullable=True)
    tracking_code = db.Column(db.String(64), nullable=True)
    carrier = db.Column(db.String(64), nullable=True)
    tracking_updated_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utcnow,
    )
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )

    __table_args__ = (
        db.UniqueConstraint(
            "remote_order_id", name="uq_remote_order_id"
        ),
        db.UniqueConstraint(
            "aliexpress_order_id", "position",
            name="uq_order_position",
        ),
        db.CheckConstraint(
            "position >= 0", name="ck_position_nonneg"
        ),
    )

    aliexpress_order = db.relationship(
        "AliExpressOrder",
        backref=db.backref(
            "remote_orders",
            cascade="all, delete-orphan",
            order_by="AliExpressRemoteOrder.position",
        ),
    )

    def __repr__(self):
        return (
            f"<AliExpressRemoteOrder {self.id} "
            f"order={self.aliexpress_order_id} "
            f"pos={self.position}>"
        )


# ==========================================================
# Item de fulfillment AliExpress (snapshot do SKU comprado)
# ==========================================================
class AliExpressOrderItem(db.Model):
    __tablename__ = "aliexpress_order_items"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    aliexpress_order_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("aliexpress_orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    order_item_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("order_items.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    product_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    variant_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("product_variants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    supplier_product_external_id = db.Column(db.String(64), nullable=False)
    supplier_sku_external_id = db.Column(db.String(64), nullable=False)
    supplier_sku_attr = db.Column(db.Text, nullable=True)
    quantity = db.Column(db.Integer, nullable=False)
    supplier_unit_cost = db.Column(db.Numeric(10, 2))
    supplier_currency = db.Column(db.String(3))
    variant_label = db.Column(db.String(255))
    state = db.Column(
        db.String(20),
        nullable=False,
        default="queued",
        server_default="queued",
    )
    last_error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(
        db.DateTime,
        default=utcnow,
        onupdate=utcnow,
    )

    aliexpress_order = db.relationship(
        "AliExpressOrder", backref=db.backref("items", lazy=True, cascade="all, delete-orphan")
    )
    order_item = db.relationship("OrderItem")
    product = db.relationship("Product")
    variant = db.relationship("ProductVariant")

    __table_args__ = (
        db.CheckConstraint("quantity > 0", name="ck_aliexpress_order_item_quantity"),
        db.CheckConstraint(
            "state IN "
            "('queued', 'blocked', 'ready', 'submitting', 'submitted', "
            "'confirmed', 'shipped', 'delivered', 'cancel_requested', "
            "'cancelled', 'failed', 'manual_review')",
            name="ck_aliexpress_order_item_state",
        ),
    )


# ==========================================================
# Log de sincronização (auditoria)
# ==========================================================
class SyncLog(db.Model):
    __tablename__ = "sync_logs"

    id = db.Column(db.BigInteger, primary_key=True)
    entity_type = db.Column(db.String(20), nullable=False)
    entity_id = db.Column(UUID(as_uuid=True), nullable=False)
    action = db.Column(db.String(30))
    status = db.Column(db.String(20))
    message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)


# ==========================================================
# Mapeamento de categoria de fornecedor (dropshipping)
# ==========================================================
class SupplierCategoryMapping(db.Model):
    __tablename__ = "supplier_category_mappings"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    supplier_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("suppliers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_category_id = db.Column(db.String(64), nullable=True)
    external_category_name = db.Column(db.String(255))
    category_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source = db.Column(
        db.String(20),
        nullable=False,
        default="internal",
        server_default="internal",
    )
    created_at = db.Column(db.DateTime, default=utcnow)

    supplier = db.relationship(
        "Supplier",
        backref=db.backref("category_mappings", lazy=True),
    )
    category = db.relationship("Category")


# ==========================================================
# Sincronização de produto (auditoria de sync)
# ==========================================================
class ProductSync(db.Model):
    __tablename__ = "product_syncs"

    id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    product_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    supplier_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("suppliers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_product_id = db.Column(db.String(64), nullable=True)
    state = db.Column(
        db.String(30),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    last_synced_at = db.Column(db.DateTime)
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(
        db.DateTime,
        default=utcnow,
        onupdate=utcnow,
    )

    product = db.relationship(
        "Product",
        backref=db.backref("syncs", lazy=True),
    )
    supplier = db.relationship(
        "Supplier",
        backref=db.backref("product_syncs", lazy=True),
    )

    __table_args__ = (
        db.UniqueConstraint(
            "product_id", "supplier_id", name="uq_product_sync"
        ),
    )
