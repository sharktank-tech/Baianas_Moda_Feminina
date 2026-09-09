from flask import has_app_context, request
from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.orm import Session

from loja.observability.mercado_pago import log_event, request_correlation_id


_INSTALLED = False


def _payment_request_fields():
    path = request.path or ""
    fields = {
        "method": request.method,
        "path": path,
        "event_type": request.args.get("type"),
        "x_request_id": request.headers.get("x-request-id"),
    }

    data_id = request.args.get("data.id")
    event_type = fields["event_type"]
    if event_type == "topic_merchant_order_wh":
        fields["merchant_order_id"] = data_id
    elif data_id:
        fields["payment_id"] = data_id

    if path.startswith("/pagamentos/retorno/"):
        fields.update(
            {
                "payment_id": request.args.get("payment_id")
                or request.args.get("collection_id"),
                "external_reference": request.args.get("external_reference")
                or request.args.get("order_id"),
                "collection_status": request.args.get("collection_status"),
            }
        )

    return fields


def install_mercado_pago_observability(app):
    """Install sanitized request and Order-state telemetry without changing flow."""
    global _INSTALLED

    @app.before_request
    def _mp_observability_before_request():
        if not request.path.startswith("/pagamentos/"):
            return None
        correlation_id = request_correlation_id()
        log_event(
            "http.payment_request.received",
            correlation_id=correlation_id,
            **_payment_request_fields(),
        )
        return None

    @app.after_request
    def _mp_observability_after_request(response):
        if request.path.startswith("/pagamentos/"):
            log_event(
                "http.payment_request.completed",
                correlation_id=request_correlation_id(),
                status_code=response.status_code,
                **_payment_request_fields(),
            )
            response.headers.setdefault(
                "X-Correlation-ID",
                request_correlation_id(),
            )
        return response

    if _INSTALLED:
        return

    @event.listens_for(Session, "before_flush")
    def _observe_order_transitions(session, flush_context, instances):
        if not has_app_context():
            return
        try:
            from loja.modules.getway_mercado_pago.payment_models import Order
        except Exception:
            return

        for obj in list(session.new) + list(session.dirty):
            if not isinstance(obj, Order):
                continue

            state = sa_inspect(obj)
            history = state.attrs.status.history
            if obj in session.new:
                old_status = None
                new_status = obj.status
            elif history.has_changes():
                old_status = history.deleted[0] if history.deleted else None
                new_status = history.added[0] if history.added else obj.status
            else:
                continue

            log_event(
                "order.status.transition",
                order_id=getattr(obj, "id", None),
                external_reference=getattr(obj, "id", None),
                old_status=old_status,
                new_status=new_status,
                mp_status=getattr(obj, "mp_status", None),
                mp_status_detail=getattr(obj, "mp_status_detail", None),
                preference_id=getattr(obj, "mp_preference_id", None),
                payment_id=getattr(obj, "mp_payment_id", None),
            )

    _INSTALLED = True
