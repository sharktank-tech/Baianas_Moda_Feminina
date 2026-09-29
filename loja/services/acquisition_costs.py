"""Observed supplier purchase amounts. No tax rates or implicit zeroes.

Amounts cover one unit, in BRL, in the same destination/variant/time scope
as the freight evidence. Retail taxes and payment fees remain separate.
"""
from decimal import Decimal, InvalidOperation


class AcquisitionCostUnavailable(ValueError):
    pass


def observed_amount(value, *, positive=False):
    if value is None or isinstance(value, bool):
        raise AcquisitionCostUnavailable("supplier_taxes_unverified")
    try:
        amount = Decimal(str(value))
        if (not amount.is_finite() or amount < 0 or amount > Decimal("99999999.99")
                or (positive and amount <= 0) or amount != amount.quantize(Decimal("0.01"))):
            raise InvalidOperation
        return amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise AcquisitionCostUnavailable("supplier_cost_invalid") from None


def acquisition_components(quote):
    """Require all components, including explicitly confirmed zero taxes."""
    return {
        "supplier_unit_cost": observed_amount(quote.get("supplier_unit_cost"), positive=True),
        "import_duty": observed_amount(quote.get("import_duty")),
        "icms": observed_amount(quote.get("icms")),
    }


def supplier_tax_total(link):
    return observed_amount(getattr(link, "import_duty", None)) + observed_amount(getattr(link, "icms", None))


def freeze_checkout_acquisition(items, dest_cep):
    """Freeze the exact DB evidence, under supplier locks, before payment.

    No network. One unit per supplier link; incomplete legacy evidence is
    rejected even in flat-shipping mode. Only AliExpress lines are changed.
    Locks remain held by the checkout transaction through order creation.
    """
    from datetime import timedelta
    from loja import db
    from loja.modules.models import Product, SupplierProduct
    from .shipping_quote import (
        QuoteUnavailable, AliExpressSupplierProvider, normalize_cep,
    )
    from .supplier_freight_quotes import find_valid_reference_quotes
    from loja.utils import utcnow

    targets = []
    for item in sorted(items, key=lambda row: str(row.get("product_id"))):
        product = (Product.query.filter_by(id=item["product_id"]).with_for_update(of=Product)
                   .populate_existing().first())
        if product is None or product.source != "aliexpress":
            continue
        links = (SupplierProduct.query.filter_by(product_id=product.id, is_active=True)
                 .order_by(SupplierProduct.id).with_for_update(of=SupplierProduct)
                 .populate_existing().all())
        if len(links) != 1:
            raise QuoteUnavailable("no_supplier_data")
        from .pricing import sale_price_for_margin, PricingError
        link = links[0]
        try:
            minimum = sale_price_for_margin(link.landed_cost, product.target_margin_percent)
            if product.pricing_status != "priced" or product.price < minimum:
                raise PricingError("pricing_stale")
            if Decimal(str(item["unit_price"])) != product.price:
                raise PricingError("cart_price_changed")
        except (PricingError, TypeError, InvalidOperation):
            raise QuoteUnavailable("supplier_cost_changed") from None
        targets.append((item, link))
    if not targets:
        return
    now = utcnow()
    try:
        quotes = find_valid_reference_quotes(dest_cep=dest_cep, for_checkout=True, now=now)
    except Exception:
        raise QuoteUnavailable("provider_unavailable") from None
    counts = {}
    for item, link in targets:
        key = str(link.id)
        counts[key] = counts.get(key, 0) + int(item["quantity"])
    for item, link in targets:
        fresh = quotes.get(str(link.id))
        problem = AliExpressSupplierProvider._check_fresh(
            fresh, normalize_cep(dest_cep), None,
            {"qty": counts[str(link.id)], "variants": {str(item.get("variant_id") or "")}}, now,
        )
        if problem:
            raise QuoteUnavailable(problem)
        if (link.currency != "BRL" or link.freight_currency != "BRL" or
                link.freight_status != "available"):
            raise QuoteUnavailable("supplier_cost_changed")
        components = acquisition_components(fresh)
        shipping = observed_amount(fresh["cost"])
        if any(components[k] != getattr(link, attr, None)
               for k, attr in (("supplier_unit_cost", "cost"), ("import_duty", "import_duty"), ("icms", "icms"))):
            raise QuoteUnavailable("supplier_cost_changed")
        landed = sum(components.values(), shipping)
        if landed != link.landed_cost or shipping != link.shipping_cost:
            raise QuoteUnavailable("supplier_cost_changed")
        item.update(
            supplier_unit_cost_snapshot=components["supplier_unit_cost"],
            supplier_shipping_cost_snapshot=shipping,
            supplier_import_duty_snapshot=components["import_duty"],
            supplier_icms_snapshot=components["icms"],
            supplier_landed_cost_snapshot=landed,
            supplier_cost_currency_snapshot="BRL",
            supplier_cost_evidence_snapshot={
                "version": 2, "supplier_product_id": str(link.id),
                "dest_cep": fresh["dest_cep"], "dest_cep_end": fresh.get("dest_cep_end"),
                "variant_scope": fresh.get("variant_scope"),
                "covers_all_variants": fresh.get("covers_all_variants", False),
                "quantity": 1, "evidence_ref_hash": fresh["evidence_ref_hash"],
                "quoted_at": fresh["quoted_at"].isoformat(),
                "expires_at": (fresh["quoted_at"] + timedelta(seconds=fresh["ttl_seconds"])).isoformat(),
            },
        )
