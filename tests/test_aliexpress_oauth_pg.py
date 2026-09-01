"""Prova de unicidade de refresh OAuth em PostgreSQL (Fase 8.0.4).

Só roda quando DATABASE_URL aponta para PostgreSQL. Usa schema descartável
`pollo_test_oauth_*` e NUNCA toca `public` nem `pollomodas`.

Dois threads tentam renovar a MESMA credencial expirada ao mesmo tempo, em
sessões distintas. O lock ``SELECT ... FOR UPDATE`` + ``populate_existing``
garante que o provider (fake) é chamado no máximo UMA vez; quem perde a corrida
reutiliza a credencial nova (desfecho idempotente, ``refreshed=False``).

Protege tb que NENHUM refresh acontece quando a credencial já está válida.
"""
import secrets
import threading
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from conftest import ORIGINAL_DATABASE_URL
from loja.integrations.aliexpress.oauth_refresh import (
    AliExpressOAuthRefresher,
    AliExpressOAuthRefreshRequest,
    AliExpressOAuthRefreshResult,
    refresh_aliexpress_credential,
)

DATABASE_URL = ORIGINAL_DATABASE_URL
try:
    IS_POSTGRES = (
        bool(DATABASE_URL)
        and make_url(DATABASE_URL).get_backend_name() == "postgresql"
    )
except Exception:
    IS_POSTGRES = False

pytestmark = pytest.mark.skipif(
    not IS_POSTGRES,
    reason="requer PostgreSQL (DATABASE_URL postgresql://)",
)


class CountingRefresher(AliExpressOAuthRefresher):
    """Provider fake thread-safe: conta chamadas, nunca faz rede."""

    def __init__(self):
        self.call_count = 0
        self._lock = threading.Lock()

    def refresh(self, request: AliExpressOAuthRefreshRequest):
        with self._lock:
            self.call_count += 1
        return AliExpressOAuthRefreshResult(
            access_token="TOKEN_PG",
            expires_in=3600,
            refresh_token="RT_PG_NOVO",
        )


@pytest.fixture
def env(monkeypatch):
    schema = "pollo_test_%s_%s" % ("oauth", secrets.token_hex(4))
    eng = create_engine(DATABASE_URL)
    with eng.connect() as c:
        c.execute(text("CREATE SCHEMA IF NOT EXISTS %s" % schema))
        c.commit()

    from cryptography.fernet import Fernet

    monkeypatch.setenv(
        "ALIEXPRESS_CREDENTIAL_KEY", Fernet.generate_key().decode("utf-8")
    )

    import config
    import loja.schema as schema_mod

    monkeypatch.setattr(config.Config, "SQLALCHEMY_DATABASE_URI", DATABASE_URL)
    monkeypatch.setattr(config.Config, "SECRET_KEY", "test-secret")
    monkeypatch.setattr(
        config.Config,
        "SQLALCHEMY_ENGINE_OPTIONS",
        {
            "pool_pre_ping": True,
            "pool_recycle": 300,
            "connect_args": {"options": "-csearch_path=%s" % schema},
        },
    )
    monkeypatch.setattr(schema_mod, "DB_SCHEMA", schema)

    from loja import create_app

    app = create_app()
    with app.app_context():
        from loja import db

        db.create_all()

    yield schema, app

    with eng.connect() as c:
        c.execute(text("DROP SCHEMA IF EXISTS %s CASCADE" % schema))
        c.commit()


def _materialize_expired_credential(app):
    """Supplier + credencial expirada com refresh_token no schema descartável."""
    from loja import db
    from loja.integrations.aliexpress.credential_store import persist_credential
    from loja.modules.models import Supplier

    with app.app_context():
        supplier = Supplier(
            id=uuid.uuid4(),
            name="Supp OAuth PG 8.0.4",
            channel_type="aliexpress",
            seller_id="SELLER_OAUTH_PG",
            default_currency="USD",
        )
        db.session.add(supplier)
        db.session.flush()
        persist_credential(
            supplier_id=supplier.id,
            seller_id=supplier.seller_id,
            access_token="TOKEN_ANTIGO",
            refresh_token="RT_PG",
            expires_in=0,
        )
        db.session.commit()
        return supplier.id


def _start_refresh(app, supplier_id, refresher, results, errors, barrier):
    from loja import db

    try:
        with app.app_context():
            db.session.connection().exec_driver_sql(
                "SET SESSION lock_timeout = 15000"
            )
            barrier.wait(timeout=30)
            out = refresh_aliexpress_credential(
                supplier_id, refresher=refresher
            )
            results.append((out.ok, out.refreshed, out.error_code))
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)


def test_two_concurrent_refreshes_produce_one_logical_refresh(env):
    schema, app = env
    supplier_id = _materialize_expired_credential(app)

    refresher = CountingRefresher()
    results = []
    errors = []
    barrier = threading.Barrier(2)

    t1 = threading.Thread(
        target=_start_refresh,
        args=(app, supplier_id, refresher, results, errors, barrier),
    )
    t2 = threading.Thread(
        target=_start_refresh,
        args=(app, supplier_id, refresher, results, errors, barrier),
    )
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert errors == [], errors
    assert all(ok for ok, _, _ in results)
    # No máximo um refresh lógico: um venceu, o outro reutilizou (idempotente).
    assert refresher.call_count == 1
    assert sorted(refreshed for _, refreshed, _ in results) == [False, True]

    from loja import db
    from loja.integrations.aliexpress.credential_store import (
        AliExpressCredential,
        _decrypt_or_raise,
        get_fulfillment_access_token,
    )

    with app.app_context():
        assert get_fulfillment_access_token(supplier_id) == "TOKEN_PG"
        cred = AliExpressCredential.query.filter_by(
            supplier_id=supplier_id
        ).first()
        assert _decrypt_or_raise(cred.refresh_token_encrypted) == "RT_PG_NOVO"
        assert cred.last_error is None