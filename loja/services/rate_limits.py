"""Janela fixa de tentativas por escopo, persistida em ``AuthRateLimit``.

Escopos em uso:

- ``pwreset_ip`` / ``pwreset_email`` — reset de senha (janela/reset);
- ``checkout`` — limite de tentativas de checkout (item 1.8).

A contagem é persistida mesmo quando a tentativa é bloqueada, para que
spam não consiga "resfriar" a janela continuando a bater.
"""

import hashlib


def _key_hash(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def rate_allow(scope: str, key: str, limit: int, window_seconds: int) -> bool:
    """Janela fixa por (escopo, chave). ``limit <= 0`` desliga o gate."""
    from loja import db
    from loja.modules.models import AuthRateLimit, utcnow

    if limit <= 0:
        return False

    now = utcnow()
    key_hash = _key_hash(key)
    row = db.session.get(AuthRateLimit, {"scope": scope, "key_hash": key_hash})
    if row is None:
        row = AuthRateLimit(
            scope=scope,
            key_hash=key_hash,
            window_started_at=now,
            attempts=0,
        )
        db.session.add(row)

    elapsed = (now - row.window_started_at).total_seconds()
    if elapsed >= window_seconds:
        row.window_started_at = now
        row.attempts = 1
        allowed = True
    elif row.attempts >= limit:
        allowed = False
    else:
        row.attempts += 1
        allowed = True

    db.session.commit()
    return allowed
