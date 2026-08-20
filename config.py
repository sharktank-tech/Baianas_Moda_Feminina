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
    SQLALCHEMY_DATABASE_URI = (
        DATABASE_URL
    )

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
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
    PAYMENT_FLAT_SHIPPING = os.environ.get(
        "PAYMENT_FLAT_SHIPPING",
        "18.90",
    )
    FREE_SHIPPING_MIN = os.environ.get(
        "FREE_SHIPPING_MIN",
        "249.00",
    )
