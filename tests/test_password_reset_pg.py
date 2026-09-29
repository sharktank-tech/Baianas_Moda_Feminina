"""Valida 0027 em PostgreSQL descartável: up idempotente, down reversível e
fluxo transacional do serviço contra PostgreSQL real.

Só roda quando DATABASE_URL (ORIGINAL, antes do conftest trocar por SQLite)
aponta para PostgreSQL — mesmo padrão de test_*_pg.py existentes.
"""

import hashlib
import re
import secrets as pysecrets
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from test_aliexpress_payment_guard_pg import (  # noqa: F401
    DATABASE_URL,
    IS_POSTGRES,
    env,
)

pytestmark = pytest.mark.skipif(
    not IS_POSTGRES,
    reason="requer PostgreSQL (DATABASE_URL postgresql://)",
)

_MIG = Path(__file__).resolve().parents[1] / "migrations"
_TOKEN_RE = re.compile(r"/redefinir-senha\?token=([A-Za-z0-9_\-]+)")

PUBLIC_MESSAGE = (
    "Se houver uma conta elegível com esse e-mail, você receberá as "
    "instruções para redefinir sua senha."
)


def _script(nome: str, schema: str) -> str:
    return (_MIG / nome).read_text(encoding="utf-8").replace(
        "pollomodas", schema
    )


def _coluna_existe(conn, schema, tabela, coluna):
    return (
        conn.execute(
            text(
                """
                SELECT count(*) FROM information_schema.columns
                 WHERE table_schema = :s AND table_name = :t
                   AND column_name = :c
                """
            ),
            {"s": schema, "t": tabela, "c": coluna},
        ).scalar_one()
        == 1
    )


def _tabela_existe(conn, schema, tabela):
    return (
        conn.execute(
            text(
                """
                SELECT count(*) FROM information_schema.tables
                 WHERE table_schema = :s AND table_name = :t
                """
            ),
            {"s": schema, "t": tabela},
        ).scalar_one()
        == 1
    )


def test_password_reset_migration_up_idempotente_e_down_reversivel(env):
    schema, _ = env
    assert schema.startswith("pollo_test_")
    up = _script("0027_password_reset.sql", schema)
    down = _script("0027_password_reset_down.sql", schema)

    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        # UP duas vezes → idempotente.
        for _ in range(2):
            conn.exec_driver_sql(up)
            conn.commit()

        assert _coluna_existe(conn, schema, "users", "auth_version")
        assert _tabela_existe(conn, schema, "password_reset_tokens")
        assert _tabela_existe(conn, schema, "auth_rate_limits")

        # Índice único parcial: 1 token não consumido por usuário.
        idx = conn.execute(
            text(
                """
                SELECT indexdef FROM pg_indexes
                 WHERE schemaname = :s
                   AND indexname = 'uq_password_reset_active_user'
                """
            ),
            {"s": schema},
        ).scalar_one_or_none()
        assert idx is not None
        assert "WHERE (used_at IS NULL)" in idx
        assert "UNIQUE" in idx

        # DOWN → remove tudo (reversível).
        conn.exec_driver_sql(down)
        conn.commit()
        assert not _coluna_existe(conn, schema, "users", "auth_version")
        assert not _tabela_existe(conn, schema, "password_reset_tokens")
        assert not _tabela_existe(conn, schema, "auth_rate_limits")

        # UP de novo → restaura.
        conn.exec_driver_sql(up)
        conn.commit()
        assert _coluna_existe(conn, schema, "users", "auth_version")
        assert _tabela_existe(conn, schema, "password_reset_tokens")
        assert _tabela_existe(conn, schema, "auth_rate_limits")


def test_fluxo_reset_transacional_em_postgres(env, monkeypatch):
    """Serviço completo em PG: envio simulado, troca atômica e rollback."""
    _, app = env
    app.config.update(
        WTF_CSRF_ENABLED=False,
        PASSWORD_RESET_SEND_ASYNC=False,
        APP_PUBLIC_URL="https://www.pollomodas.test",
        PASSWORD_RESET_IP_LIMIT=1000,
        PASSWORD_RESET_EMAIL_LIMIT=1000,
    )

    with app.app_context():
        from loja import db
        from loja.modules.models import PasswordResetToken, User, utcnow
        from loja.services.password_reset import (
            PUBLIC_MESSAGE as MSG,
            request_password_reset,
            reset_password_with_token,
            validate_reset_token,
        )

        user = User(
            name="PG reset",
            email=f"reset-{pysecrets.token_hex(4)}@pg.test",
            phone="11999999999",
            is_admin=False,
        )
        user.set_password("senha123")
        db.session.add(user)
        db.session.commit()
        user_id = user.id

        enviado = []

        def _captura(dest, assunto, corpo, corpo_html=None):
            enviado.append(corpo)

        msg = request_password_reset(
            user.email, ip="10.9.8.7", email_sender=_captura
        )
        assert msg == MSG == PUBLIC_MESSAGE
        assert len(enviado) == 1
        token = _TOKEN_RE.search(enviado[0]).group(1)

        # Falha parcial → rollback: token NÃO consumido, senha NÃO muda.
        original = User.set_password
        estado = {"falhou": False}

        def _quebra(self, senha):
            if not estado["falhou"]:
                estado["falhou"] = True
                raise RuntimeError("falha simulada de gravação")
            original(self, senha)

        monkeypatch.setattr(User, "set_password", _quebra)
        assert reset_password_with_token(token, "NovaSenha456") == (
            False,
            "invalid",
        )
        assert validate_reset_token(token) == "ok"
        db.session.expire_all()
        assert (
            db.session.get(User, user_id).check_password("senha123") is True
        )

        # Transação completa → troca + auth_version + consumo do token.
        monkeypatch.undo()
        assert reset_password_with_token(token, "NovaSenha456") == (True, "ok")
        db.session.expire_all()

        atual = db.session.get(User, user_id)
        assert atual.check_password("NovaSenha456")
        assert atual.auth_version == 2

        linhas = PasswordResetToken.query.filter_by(user_id=user_id).all()
        assert len(linhas) == 1
        assert linhas[0].used_at is not None
        assert linhas[0].token_hash == hashlib.sha256(
            token.encode()
        ).hexdigest()

        # Reuso do mesmo token → "used", nunca erro.
        assert reset_password_with_token(token, "OutraSenha789") == (
            False,
            "used",
        )

        # Janela de validade gravada coerente (30 min).
        assert linhas[0].expires_at > linhas[0].created_at
        assert linhas[0].created_at <= utcnow()
