from datetime import datetime, timezone
from zoneinfo import ZoneInfo

#: Fuso de exibição da loja. datetimes do banco são naive em UTC (ver utcnow).
SAO_PAULO_TZ = ZoneInfo("America/Sao_Paulo")


def utcnow():
    """Retorna a data/hora UTC atual no mesmo formato usado pelas colunas
    (naive, sem timezone) — substitui o datetime.utcnow() deprecado."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_sao_paulo(value):
    """Converte um datetime para America/Sao_Paulo.

    Valores naive são interpretados como UTC (formato das colunas);
    valores aware são convertidos. Retorna None para entrada None.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(SAO_PAULO_TZ)


def format_deadline_sp(value):
    """Formata um término de promoção para exibição (horário de São Paulo).

    Retorna "" quando não há prazo, para o template omitir o relógio.
    """
    local = as_sao_paulo(value)
    if local is None:
        return ""
    return local.strftime("%d/%m às %H:%M")