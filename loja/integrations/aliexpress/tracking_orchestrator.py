"""Orquestração gated de tracking pós-venda (FASE 8.3.0-E).

O gate controla a operação completa. Desligado, o provider nem é construído.
Ligado, o caller ainda precisa injetar explicitamente um provider; não existe
fallback que leia credenciais ou faça HTTP implicitamente.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

from .tracking import (
    AliExpressTrackingPersistenceResult,
    AliExpressTrackingProvider,
    AliExpressTrackingUnavailable,
    evaluate_tracking_observations,
    persist_sanitized_tracking,
)


TRACKING_SYNC_ENABLE_ENV = "ALIEXPRESS_TRACKING_SYNC_ENABLED"
_ELIGIBLE_STATES = frozenset(
    {"submitted", "confirmed", "shipped", "delivered"}
)


@dataclass(frozen=True)
class AliExpressTrackingOrchestrationResult:
    state: str
    persisted: int = 0
    reason: Optional[str] = None


def is_tracking_sync_enabled() -> bool:
    return os.environ.get(TRACKING_SYNC_ENABLE_ENV) == "1"


def sync_and_persist_aliexpress_tracking(
    ali_order,
    *,
    provider: Optional[AliExpressTrackingProvider] = None,
    provider_factory: Optional[Callable[[], AliExpressTrackingProvider]] = None,
    session=None,
) -> AliExpressTrackingOrchestrationResult:
    """Consulta e persiste tracking sem retry e sem esconder ambiguidades."""
    if not is_tracking_sync_enabled():
        return AliExpressTrackingOrchestrationResult(
            "blocked", reason="tracking_sync_hard_off"
        )

    state = str(getattr(ali_order, "state", "") or "")
    if state not in _ELIGIBLE_STATES:
        return AliExpressTrackingOrchestrationResult(
            "blocked", reason="tracking_sync_state_not_eligible"
        )

    remote_orders = tuple(getattr(ali_order, "remote_orders", None) or ())
    if not remote_orders:
        return AliExpressTrackingOrchestrationResult(
            "blocked", reason="tracking_remote_orders_missing"
        )

    if provider is not None and provider_factory is not None:
        return AliExpressTrackingOrchestrationResult(
            "blocked", reason="tracking_provider_ambiguous"
        )
    if provider is None:
        if provider_factory is None:
            return AliExpressTrackingOrchestrationResult(
                "blocked", reason="tracking_provider_required"
            )
        provider = provider_factory()
    if not isinstance(provider, AliExpressTrackingProvider):
        return AliExpressTrackingOrchestrationResult(
            "blocked", reason="tracking_provider_invalid"
        )

    try:
        observations = provider.fetch_tracking(len(remote_orders))
        snapshot = evaluate_tracking_observations(
            observations, expected_count=len(remote_orders)
        )
    except AliExpressTrackingUnavailable:
        return AliExpressTrackingOrchestrationResult(
            "failed", reason="tracking_provider_unavailable"
        )
    except Exception:
        return AliExpressTrackingOrchestrationResult(
            "failed", reason="tracking_provider_error"
        )

    if snapshot.reason not in {None, "tracking_pending"}:
        return AliExpressTrackingOrchestrationResult(
            "failed", reason=snapshot.reason
        )

    persistence: AliExpressTrackingPersistenceResult = (
        persist_sanitized_tracking(
            ali_order,
            snapshot,
            session=session,
        )
    )
    if not persistence.accepted:
        return AliExpressTrackingOrchestrationResult(
            "failed", reason=persistence.reason
        )
    return AliExpressTrackingOrchestrationResult(
        "completed",
        persisted=persistence.persisted,
        reason=snapshot.reason,
    )
