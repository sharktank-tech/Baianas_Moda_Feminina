"""Item 1.8: limite de tentativas de checkout por usuário (janela fixa).

Toda tentativa que chega à criação do pedido em
``/pagamentos/mercado-pago`` (depois do pre-check idempotente, que não
tem efeito colateral) consome 1 tentativa do usuário autenticado. Ao
estourar a janela, a rota bloqueia antes de qualquer efeito colateral
(sem pedido, sem reserva de estoque, sem chamada ao provedor).

Config (app.config / env):

- ``CHECKOUT_RATE_LIMIT`` — tentativas por janela; **0 desliga o gate**;
- ``CHECKOUT_RATE_WINDOW_SECONDS`` — tamanho da janela em segundos.

A contagem persiste em ``AuthRateLimit`` (escopo ``checkout``) — a mesma
tabela da janela de reset de senha (migration 0027), sem migration nova.
"""

from loja.services.rate_limits import rate_allow

CHECKOUT_RATE_SCOPE = "checkout"
DEFAULT_CHECKOUT_RATE_LIMIT = 10
DEFAULT_CHECKOUT_RATE_WINDOW_SECONDS = 300
MAX_CHECKOUT_RATE_LIMIT = 1000
MAX_CHECKOUT_RATE_WINDOW_SECONDS = 86400

CHECKOUT_BLOCK_MESSAGE = (
    "Muitas tentativas de pagamento em pouco tempo. "
    "Aguarde alguns minutos e tente novamente."
)


def _bounded_config_int(name, default, maximum):
    from flask import current_app

    try:
        value = int(current_app.config.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(0, min(maximum, value))


def checkout_rate_settings():
    """``(limit, window_seconds)`` já normalizados; limit 0 = gate OFF."""
    limit = _bounded_config_int(
        "CHECKOUT_RATE_LIMIT",
        DEFAULT_CHECKOUT_RATE_LIMIT,
        MAX_CHECKOUT_RATE_LIMIT,
    )
    window = _bounded_config_int(
        "CHECKOUT_RATE_WINDOW_SECONDS",
        DEFAULT_CHECKOUT_RATE_WINDOW_SECONDS,
        MAX_CHECKOUT_RATE_WINDOW_SECONDS,
    )
    if window <= 0:
        window = DEFAULT_CHECKOUT_RATE_WINDOW_SECONDS
    return limit, window


def checkout_attempt_allowed(user_id) -> bool:
    """Consome 1 tentativa do usuário. ``False`` = janela estourada.

    ``CHECKOUT_RATE_LIMIT=0`` desliga o gate sem tocar no banco (escape
    operacional, mesmo padrão dos gates OFF do projeto).
    """
    limit, window = checkout_rate_settings()
    if limit <= 0:
        return True
    return rate_allow(CHECKOUT_RATE_SCOPE, str(user_id), limit, window)
