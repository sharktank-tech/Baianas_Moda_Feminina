"""FASE 8.1.0 — política de habilitação do fulfillment AliExpress de produção.

Este módulo NÃO executa submissão e NÃO é conectado automaticamente ao webhook
de pagamento neste checkpoint. Ele apenas define a política fail-closed que será
usada pelo pipeline de produção nas próximas etapas.

Para um provider real ficar selecionável, DUAS travas independentes precisam
estar explicitamente ligadas:

* ``ALIEXPRESS_PRODUCTION_FULFILLMENT_ENABLED=1`` — autoriza o pipeline de
  produção da FASE 8.1.0;
* ``ALIEXPRESS_PLACEORDER_ENABLED=1`` — autoriza o boundary HTTP de placeorder.

Qualquer valor ausente/diferente de ``1`` mantém ``HardOffOrderSubmitter``.
Assim ambientes com gates históricos/canários ainda configurados não passam a
comprar automaticamente apenas por receber este código.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .placeorder_provider import PLACEORDER_ENABLED_ENV, get_real_order_submitter
from .submission import AliExpressOrderSubmitter, HardOffOrderSubmitter


PRODUCTION_FULFILLMENT_ENABLED_ENV = "ALIEXPRESS_PRODUCTION_FULFILLMENT_ENABLED"


@dataclass(frozen=True)
class ProductionFulfillmentGateState:
    production_enabled: bool
    placeorder_enabled: bool

    @property
    def can_select_real_provider(self) -> bool:
        return self.production_enabled and self.placeorder_enabled


def _exact_env_enabled(name: str) -> bool:
    return os.environ.get(name) == "1"


def production_fulfillment_gate_state() -> ProductionFulfillmentGateState:
    """Retorna apenas flags booleanas; nunca expõe segredos/config sensível."""
    return ProductionFulfillmentGateState(
        production_enabled=_exact_env_enabled(PRODUCTION_FULFILLMENT_ENABLED_ENV),
        placeorder_enabled=_exact_env_enabled(PLACEORDER_ENABLED_ENV),
    )


def get_production_order_submitter() -> AliExpressOrderSubmitter:
    """Seleciona provider real somente sob dupla autorização operacional.

    Não há fallback parcial: se QUALQUER gate estiver desligado, retorna hard-off.
    O provider real ainda revalida ``ALIEXPRESS_PLACEORDER_ENABLED`` dentro de
    ``submit()``, mantendo defesa em profundidade antes do HTTP.
    """
    gates = production_fulfillment_gate_state()
    if not gates.can_select_real_provider:
        return HardOffOrderSubmitter()
    return get_real_order_submitter()


__all__ = [
    "PRODUCTION_FULFILLMENT_ENABLED_ENV",
    "ProductionFulfillmentGateState",
    "production_fulfillment_gate_state",
    "get_production_order_submitter",
]
