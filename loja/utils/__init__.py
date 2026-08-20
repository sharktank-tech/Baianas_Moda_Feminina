from datetime import datetime, timezone


def utcnow():
    """Retorna a data/hora UTC atual no mesmo formato usado pelas colunas
    (naive, sem timezone) — substitui o datetime.utcnow() deprecado."""
    return datetime.now(timezone.utc).replace(tzinfo=None)