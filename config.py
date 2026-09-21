import os
from dotenv import load_dotenv

def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

load_dotenv()
class Config:
    # ============== Flask =================
    SECRET_KEY = os.getenv("SECRET_KEY")
    FLASK_APP = os.getenv("FLASK_APP", "run")

    if not SECRET_KEY:
        raise RuntimeError("SECRET_KEY não configurada.")

    # ============== Database =================
    DATABASE_URL = os.getenv("DATABASE_URL")

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL não configurada.")

    # ============= SQLAlchemy ==================
    SQLALCHEMY_DATABASE_URI = DATABASE_URL

    # Schema dedicado da PolloModas. Tudo (create_all, ensure_schema, sessões e
    # queries) ocorre dentro dele; `public` pertence a outro sistema e é
    # intocável. Em testes isolados (ex.: pollo_smoke_xxx) sobrescreva via env
    # POLLOMODAS_SCHEMA — mas NUNCA aponte para public/pollomodas em produção.
    POLLOMODAS_SCHEMA = os.getenv("POLLOMODAS_SCHEMA", "pollomodas")

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # search_path: em PostgreSQL usamos options=-csearch_path=<POLLOMODAS_SCHEMA>
    # como DICA para conexão direta / pooling de sessão. PORÉM o Session Pooler
    # do Supabase em transaction pooling IGNORA essa option, por isso o mecanismo
    # definitivo é o hook de transação (SET LOCAL search_path) instalado em
    # loja/__init__.py (create_app), que força o schema a cada transação. Nunca
    # há fallback para public. Apenas PostgreSQL; SQLite (testes) ignora options.
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }
    if DATABASE_URL.startswith("postgresql"):
        SQLALCHEMY_ENGINE_OPTIONS["connect_args"] = {
            "options": "-csearch_path=%s" % POLLOMODAS_SCHEMA,
        }

    # ============= Email ==================
    EMAIL_DESTINE = os.getenv("EMAIL_DESTINE")
    EMAIL_SERVER = os.getenv("EMAIL_SERVER")
    EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
    EMAIL_PORT = int(os.getenv("EMAIL_PORT", 587))
    EMAIL_SENDER = os.getenv("EMAIL_SENDER")

    # ===== Mercado Pago ====================
    MERCADO_PAGO_ACCESS_TOKEN = os.environ.get(
        "MERCADO_PAGO_ACCESS_TOKEN",
        "",
    )
    MERCADO_PAGO_WEBHOOK_SECRET = os.environ.get(
        "MERCADO_PAGO_WEBHOOK_SECRET",
        "",
    )
    APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "").rstrip("/")
    MERCADO_PAGO_USE_SANDBOX = env_bool(
        "MERCADO_PAGO_USE_SANDBOX",
        True,
    )
    MERCADO_PAGO_REQUIRE_WEBHOOK_SIGNATURE = env_bool(
        "MERCADO_PAGO_REQUIRE_WEBHOOK_SIGNATURE",
        True,
    )
    # Frete por cotação (loja/services/shipping_quote.py) atrás de flag
    # desligada por padrão: sem SHIPPING_ORIGIN_CEP/tabela real e sem
    # SHIPPING_ALIEXPRESS_FRESH_QUOTES, todo checkout real bloquearia.
    # Enquanto a integração real não existir, o checkout usa a tarifa
    # comercial temporária única SHIPPING_TEMPORARY_FLAT_RATE — regra
    # assumida pela loja, não cotação por CEP, sem frete grátis automático.
    SHIPPING_GROUPED_QUOTE_ENABLED = env_bool(
        "SHIPPING_GROUPED_QUOTE_ENABLED",
        False,
    )
    # Tarifa comercial temporária (não é cálculo por CEP). Cobrada igual
    # para todo pedido; pode divergir do custo real do fornecedor. O serviço
    # aceita somente valores entre 0 e 9999.99; o teto é uma proteção
    # operacional, não política de preço. Fretes maiores exigem cotação real.
    SHIPPING_TEMPORARY_FLAT_RATE = os.environ.get(
        "SHIPPING_TEMPORARY_FLAT_RATE",
        "10.00",
    )
    SHIPPING_ORIGIN_CEP = os.environ.get("SHIPPING_ORIGIN_CEP") or None
    SHIPPING_DOMESTIC_RATES = os.environ.get("SHIPPING_DOMESTIC_RATES") or None
    # Liberação agendada de reservas vencidas. Fail-closed: o cron só muta
    # estoque quando explicitamente habilitado; a ação admin continua sendo
    # uma operação humana autenticada e protegida por CSRF.
    STOCK_RESERVATION_RELEASE_ENABLED = env_bool(
        "STOCK_RESERVATION_RELEASE_ENABLED",
        False,
    )
    STOCK_RESERVATION_HOURS = os.environ.get(
        "STOCK_RESERVATION_HOURS",
        "24",
    )
    STOCK_RESERVATION_RELEASE_LIMIT = os.environ.get(
        "STOCK_RESERVATION_RELEASE_LIMIT",
        "100",
    )
