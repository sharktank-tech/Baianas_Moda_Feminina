"""Regressão: bootstrap de singletons em /admin deve gerar UUIDs válidos.

Cobrindo o bug `PythonUUID.uuid4()` (UUID não tem método uuid4) que causava
500 em primeiro acesso ao /admin quando SiteConfig/FooterConfig/ContactInfo
ainda não existiam.
"""

from __future__ import annotations

import uuid as _uuid

import pytest
from werkzeug.security import generate_password_hash

from loja import db
from loja.modules.models import ContactInfo, FooterConfig, SiteConfig, User


def test_admin_bootstrap_creates_singletons(app):
    client = app.test_client()

    # Garante tabelas singleton vazias antes do GET /admin.
    with app.app_context():
        SiteConfig.query.delete()
        FooterConfig.query.delete()
        ContactInfo.query.delete()
        db.session.commit()

        admin = User(
            name="Admin",
            email=f"admin-{_uuid.uuid4().hex[:8]}@test.com",
            password=generate_password_hash("secret"),
            is_admin=True,
        )
        db.session.add(admin)
        db.session.commit()
        admin_id = admin.id

    # Login do admin via sessão (sem rota extra).
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin_id)
        sess["_fresh"] = True

    r = client.get("/admin")
    assert r.status_code == 200

    try:
        with app.app_context():
            sc = SiteConfig.query.first()
            fc = FooterConfig.query.first()
            ci = ContactInfo.query.first()
            assert sc is not None, "SiteConfig não foi criado"
            assert fc is not None, "FooterConfig não foi criado"
            assert ci is not None, "ContactInfo não foi criado"
            # IDs são UUID válidos (não quebrou em PythonUUID.uuid4()).
            _uuid.UUID(str(sc.id))
            _uuid.UUID(str(fc.id))
            _uuid.UUID(str(ci.id))
    finally:
        # Não polui o BD de sessão compartilhado (outros testes assumem
        # que os singletons não existem inicialmente).
        with app.app_context():
            SiteConfig.query.delete()
            FooterConfig.query.delete()
            ContactInfo.query.delete()
            db.session.commit()
