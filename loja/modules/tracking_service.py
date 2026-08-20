from datetime import datetime

from loja import db
from loja.modules.models import Shipment, TrackingEvent


class TrackingService:
    @staticmethod
    def register_tracking(
        order,
        tracking_code,
        carrier=None,
        provider="manual",
        estimated_delivery=None
    ):
        tracking_code = tracking_code.strip().upper()

        existing_shipment = Shipment.query.filter_by(
            tracking_code=tracking_code
        ).first()

        if existing_shipment:
            raise ValueError("Este código de rastreio já está cadastrado.")

        shipment = Shipment(
            order_id=order.id,
            tracking_code=tracking_code,
            carrier=carrier,
            provider=provider,
            estimated_delivery=estimated_delivery,
            status="aguardando_postagem"
        )

        db.session.add(shipment)
        db.session.flush()

        first_event = TrackingEvent(
            shipment_id=shipment.id,
            status="aguardando_postagem",
            description="Código de rastreio cadastrado. Aguardando postagem.",
            event_at=datetime.utcnow()
        )

        db.session.add(first_event)
        db.session.commit()

        return shipment

    @staticmethod
    def update_status(
        shipment,
        status,
        description,
        location=None,
        event_at=None
    ):
        event_at = event_at or datetime.utcnow()

        latest_event = (
            TrackingEvent.query
            .filter_by(shipment_id=shipment.id)
            .order_by(TrackingEvent.event_at.desc())
            .first()
        )

        # Evita eventos duplicados.
        if (
            latest_event
            and latest_event.status == status
            and latest_event.description == description
            and latest_event.event_at == event_at
        ):
            return latest_event

        event = TrackingEvent(
            shipment_id=shipment.id,
            status=status,
            description=description,
            location=location,
            event_at=event_at
        )

        shipment.status = status
        shipment.last_checked_at = datetime.utcnow()

        db.session.add(event)
        db.session.commit()

        return event