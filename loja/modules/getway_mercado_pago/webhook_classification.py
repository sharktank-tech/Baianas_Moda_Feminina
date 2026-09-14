"""Classificação pura de notificações do Mercado Pago (issue #50).

Separa o que chega em ``POST /pagamentos/webhook`` em três categorias, ANTES
de qualquer validação de assinatura:

- ``modern_webhook``: contrato atual do Checkout Pro — ``data.id`` presente no
  query string. É o único formato com autoridade para sincronizar pagamentos.
- ``legacy_ipn``: formato IPN legado (``id`` + ``topic`` no query string) sem
  ``data.id`` e sem ``type``. Não permite validar origem pela chave secreta;
  deve ser reconhecido e ignorado com segurança.
- ``malformed_or_unknown``: tudo o mais (topic desconhecido, id ausente,
  combinações parciais de id/topic/type, payload incoerente). Nunca é
  processado nem consulta o provedor.

Regra inegociável: qualquer notificação com ``data.id`` no query string é
tratada como moderno. Combinações parciais de ``id``/``topic``/``type`` NUNCA
escapam para o caminho legado nem para o caminho moderno sem assinatura.
"""

from __future__ import annotations

from typing import Mapping, Optional


class WebhookKind:
    MODERN = "modern_webhook"
    LEGACY_IPN = "legacy_ipn"
    MALFORMED = "malformed_or_unknown"


ALLOWED_LEGACY_TOPICS = frozenset({"payment", "merchant_order"})


def _clean(value) -> str:
    return str(value or "").strip()


def classify_webhook(query_args: Mapping, body: Optional[Mapping] = None) -> str:
    """Classifica a notificação a partir do query string (função pura).

    ``query_args`` pode ser um ``request.args`` (ImmutableMultiDict) ou um
    dict simples; ``body`` é usado apenas como pista auxiliar — nunca como
    fonte de decisão de formato moderno (um ``id`` no corpo não torna a
    notificação moderna).
    """
    data_id = _clean(query_args.get("data.id"))
    if data_id:
        return WebhookKind.MODERN

    topic = _clean(query_args.get("topic")).lower()
    resource_id = _clean(query_args.get("id"))
    query_type = _clean(query_args.get("type"))

    # Uma notificação com `type` mas sem `data.id` é uma forma paralela de
    # webhook moderno parcial — não é IPN legado confiável.
    if query_type:
        return WebhookKind.MALFORMED

    if resource_id and topic in ALLOWED_LEGACY_TOPICS:
        return WebhookKind.LEGACY_IPN

    return WebhookKind.MALFORMED


def is_modern_webhook(query_args: Mapping, body: Optional[Mapping] = None) -> bool:
    return classify_webhook(query_args, body) == WebhookKind.MODERN


__all__ = [
    "ALLOWED_LEGACY_TOPICS",
    "WebhookKind",
    "classify_webhook",
    "is_modern_webhook",
]