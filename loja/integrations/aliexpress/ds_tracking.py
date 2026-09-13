"""Adapter DS read-only para tracking AliExpress (FASE 8.3.0-D).

Usa o endpoint já provado aliexpress.trade.ds.order.get. O cliente precisa ser
explicitamente injetado; este módulo não lê credenciais nem se torna provider
padrão. Apenas logistics_no e logistics_service atravessam a sanitização.
"""
from __future__ import annotations

from typing import Any, Iterable, Tuple

from .ds_order_status import DS_ORDER_STATUS_METHOD
from .status_sync import AliExpressOrderStatusSyncError
from .tracking import (
    AliExpressTrackingObservation,
    AliExpressTrackingProvider,
    AliExpressTrackingUnavailable,
    evaluate_tracking_observations,
)


_ID_ALLOWED = frozenset(
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-_"
)


def _validated_remote_order_id(value: Any) -> str:
    text = str(value or "").strip()
    if not (6 <= len(text) <= 64) or not all(char in _ID_ALLOWED for char in text):
        raise AliExpressTrackingUnavailable("tracking_remote_identity_invalid")
    return text


def _result_object(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise AliExpressTrackingUnavailable("tracking_payload_invalid")
    data = payload
    for _ in range(2):
        inner = data.get("result")
        if isinstance(inner, dict):
            data = inner
        else:
            break
    return data


def _logistics_entries(payload: Any) -> Tuple[dict, ...]:
    data = _result_object(payload)
    raw = data.get("logistics_info_list")
    if isinstance(raw, dict):
        raw = raw.get("logistics_info") or raw.get("logistics_info_list")
    if raw is None:
        return ()
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise AliExpressTrackingUnavailable("tracking_payload_invalid")
    return tuple(raw)


def extract_ds_tracking(payload: Any) -> AliExpressTrackingObservation:
    """Extrai no máximo uma identidade de tracking por pedido remoto."""
    entries = _logistics_entries(payload)
    candidates = []
    for item in entries:
        code = item.get("logistics_no")
        carrier = item.get("logistics_service") or item.get(
            "logistics_service_name"
        )
        if code is None and carrier is None:
            continue
        candidates.append(AliExpressTrackingObservation(code, carrier))

    if not candidates:
        return AliExpressTrackingObservation(None, None)
    if len(candidates) != 1:
        raise AliExpressTrackingUnavailable("tracking_payload_ambiguous")

    snapshot = evaluate_tracking_observations(candidates, expected_count=1)
    if not snapshot.complete:
        raise AliExpressTrackingUnavailable(
            snapshot.reason or "tracking_payload_invalid"
        )
    return snapshot.observations[0]


class AliExpressDSTrackingProvider(AliExpressTrackingProvider):
    """Adapter explícito; nunca é construído pelo default hard-off."""

    METHOD = DS_ORDER_STATUS_METHOD

    def __init__(self, *, client: Any, remote_order_ids: Iterable[str]):
        if client is None:
            raise AliExpressTrackingUnavailable("tracking_client_required")
        self._client = client
        self._remote_order_ids = tuple(
            _validated_remote_order_id(value) for value in remote_order_ids
        )

    def fetch_tracking(self, remote_order_count: int):
        if (
            remote_order_count <= 0
            or remote_order_count != len(self._remote_order_ids)
        ):
            raise AliExpressTrackingUnavailable(
                "tracking_cardinality_mismatch"
            )

        observations = []
        for remote_order_id in self._remote_order_ids:
            try:
                payload = self._client.call(
                    self.METHOD,
                    single_order_query={"order_id": remote_order_id},
                )
            except AliExpressOrderStatusSyncError:
                raise
            except Exception:
                raise AliExpressTrackingUnavailable(
                    "tracking_provider_error"
                ) from None
            observations.append(extract_ds_tracking(payload))
        return tuple(observations)


__all__ = [
    "AliExpressDSTrackingProvider",
    "extract_ds_tracking",
]
