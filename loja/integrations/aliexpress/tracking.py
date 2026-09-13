"""Contrato sanitizado de rastreio pós-venda (FASE 8.3.0-A).

Nenhum provider real é habilitado aqui. O boundary padrão permanece HARD-OFF e
as observações só atravessam o domínio após validação estrita. Persistência,
polling e exposição ao cliente pertencem aos checkpoints seguintes.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

from loja import db
from loja.modules.models import AliExpressOrder
from loja.utils import utcnow

_TRACKING_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{4,63}$")
_CARRIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class AliExpressTrackingUnavailable(Exception):
    """Consulta de rastreio indisponível; nunca contém resposta remota."""


@dataclass(frozen=True)
class AliExpressTrackingObservation:
    tracking_code: Optional[str]
    carrier: Optional[str]


@dataclass(frozen=True)
class AliExpressTrackingSnapshot:
    observations: Tuple[AliExpressTrackingObservation, ...]
    complete: bool
    reason: Optional[str] = None


@dataclass(frozen=True)
class AliExpressTrackingPersistenceResult:
    persisted: int
    reason: Optional[str] = None

    @property
    def accepted(self) -> bool:
        return self.reason is None


class AliExpressTrackingProvider(ABC):
    @abstractmethod
    def fetch_tracking(
        self, remote_order_count: int
    ) -> Iterable[AliExpressTrackingObservation]:
        """Devolve uma observação por pedido remoto, na ordem persistida."""


class HardOffAliExpressTrackingProvider(AliExpressTrackingProvider):
    def fetch_tracking(self, remote_order_count: int):
        raise AliExpressTrackingUnavailable("aliexpress_tracking_hard_off")


def get_default_tracking_provider() -> AliExpressTrackingProvider:
    return HardOffAliExpressTrackingProvider()


def _sanitize_observation(value) -> Optional[AliExpressTrackingObservation]:
    if not isinstance(value, AliExpressTrackingObservation):
        return None
    code = value.tracking_code
    carrier = value.carrier
    if code is None and carrier is None:
        return AliExpressTrackingObservation(None, None)
    if not isinstance(code, str) or not _TRACKING_RE.fullmatch(code):
        return None
    if carrier is not None and (
        not isinstance(carrier, str) or not _CARRIER_RE.fullmatch(carrier)
    ):
        return None
    return AliExpressTrackingObservation(code, carrier)


def evaluate_tracking_observations(
    observations: Iterable[AliExpressTrackingObservation],
    *,
    expected_count: int,
) -> AliExpressTrackingSnapshot:
    """Valida cardinalidade e conteúdo sem inventar rastreio ausente."""
    values = tuple(observations)
    if expected_count <= 0 or len(values) != expected_count:
        return AliExpressTrackingSnapshot(
            (), False, "tracking_cardinality_mismatch"
        )
    sanitized = tuple(_sanitize_observation(value) for value in values)
    if any(value is None for value in sanitized):
        return AliExpressTrackingSnapshot((), False, "tracking_invalid")
    safe = tuple(value for value in sanitized if value is not None)
    complete = all(value.tracking_code is not None for value in safe)
    return AliExpressTrackingSnapshot(
        safe,
        complete,
        None if complete else "tracking_pending",
    )


def fetch_sanitized_tracking(
    *,
    remote_order_count: int,
    provider: Optional[AliExpressTrackingProvider] = None,
) -> AliExpressTrackingSnapshot:
    active_provider = provider or get_default_tracking_provider()
    return evaluate_tracking_observations(
        active_provider.fetch_tracking(remote_order_count),
        expected_count=remote_order_count,
    )


def persist_sanitized_tracking(
    ali_order: AliExpressOrder,
    snapshot: AliExpressTrackingSnapshot,
    *,
    session=None,
) -> AliExpressTrackingPersistenceResult:
    """Persiste tracking 0/1/N atomicamente, sem sobrescrever divergências."""
    session = session or db.session
    if snapshot.reason not in {None, "tracking_pending"}:
        return AliExpressTrackingPersistenceResult(0, "tracking_snapshot_invalid")

    order = (
        session.query(AliExpressOrder)
        .filter_by(id=ali_order.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if order is None:
        raise ValueError("aliexpress_order_not_found")

    remote_orders = sorted(order.remote_orders, key=lambda item: item.position)
    if not remote_orders or len(remote_orders) != len(snapshot.observations):
        session.rollback()
        return AliExpressTrackingPersistenceResult(
            0, "tracking_cardinality_mismatch"
        )

    for remote, observation in zip(remote_orders, snapshot.observations):
        if (
            observation.tracking_code is not None
            and remote.tracking_code is not None
            and remote.tracking_code != observation.tracking_code
        ):
            session.rollback()
            return AliExpressTrackingPersistenceResult(
                0, "tracking_identity_conflict"
            )

    now = utcnow()
    persisted = 0
    for remote, observation in zip(remote_orders, snapshot.observations):
        if observation.tracking_code is None:
            continue
        changed = (
            remote.tracking_code != observation.tracking_code
            or remote.carrier != observation.carrier
        )
        if changed:
            remote.tracking_code = observation.tracking_code
            remote.carrier = observation.carrier
            remote.tracking_updated_at = now
            remote.updated_at = now
            persisted += 1

    order.updated_at = now
    session.commit()
    return AliExpressTrackingPersistenceResult(persisted)
