"""Evidência persistida para exibir confirmação; sem chamadas ou escritas."""

from decimal import Decimal, InvalidOperation


def payment_is_confirmed(order):
    """Não transforma estado interno/retorno do navegador em prova de pagamento."""
    if order is None or order.mp_status != "approved" or order.paid_at is None:
        return False
    try:
        total = Decimal(str(order.total))
        return any(
            payment.provider == "mercado_pago"
            and payment.provider_payment_id == order.mp_payment_id
            and payment.status == "approved"
            and payment.currency == "BRL"
            and Decimal(str(payment.amount)) == total
            for payment in order.payments
        )
    except (InvalidOperation, TypeError, ValueError):
        return False
