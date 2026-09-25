"""Catálogo administrativo AliExpress (Fase 7).

 GET  /admin/integrations/aliexpress                             -> status + busca
 POST /admin/integrations/aliexpress/buscar                      -> busca read-only
 GET  /admin/integrations/aliexpress/<product_id>/detalhe        -> detalhe read-only
 POST /admin/integrations/aliexpress/<product_id>/importar       -> import como rascunho
 GET  /admin/integrations/aliexpress/rascunho/<product_id>       -> draft (estado)
 POST /admin/integrations/aliexpress/rascunho/<product_id>/frete -> frete manual
 POST /admin/integrations/aliexpress/rascunho/<product_id>/margem-> precificação (serviço)
 POST /admin/integrations/aliexpress/rascunho/<product_id>/publicar -> publicação (serviço)
 POST /admin/integrations/aliexpress/encerrar                    -> limpa cookie admin

Segurança:
  * Todas as rotas exigem @admin_required (Flask-Login) + credencial admin
    criptografada (admin_session) com TTL curto; env ausente -> 500 fail-closed.
  * Nunca persiste access_token (nem em auth do Supplier); cliente construído
    por request a partir do cookie criptografado.
  * Nunca publica diretamente (product.is_active = True): publicação passa
    sempre por loja.services.publication.publish_product. Nenhuma chamada de
    freight API (frete é informado manualmente pelo admin).
  * Logs apenas contagens/status/moeda; nunca tokens, code, state, sign,
    seller_id, product_id/sku_id nem valores do fornecedor.
  * Nenhum POST escreve sem validação; frete negativo/zero-e-divertido e
    divergência de moeda são rejeitados (serviço de pricing é a autoridade).
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Optional, Tuple

from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)
from sqlalchemy.orm import selectinload

from loja import db
from loja.integrations.aliexpress import AliExpressClient
from loja.integrations.aliexpress import admin_session
from loja.integrations.aliexpress.errors import (
    AliExpressAPIError,
    AliExpressConfigError,
    AliExpressTransportError,
)
from loja.integrations.aliexpress.importer import (
    FREIGHT_AVAILABLE,
    DraftImportError,
    import_product_draft,
)
from loja.modules.models import Product
from loja.modules.services import get_layout_data
from loja.services.pricing import (
    PRICED,
    STALE,
    PricingBlocked,
    PricingError,
    calculate_landed_cost,
    price_dropshipping_product,
)
from loja.services.publication import PublicationBlocked, publish_product
from loja.utils.decorators import admin_required

aliexpress_admin_blueprint = Blueprint(
    "aliexpress_admin", __name__, url_prefix=admin_session.ADMIN_CATALOG_PATH
)

SEARCH_LOCALE = "en_US"
SEARCH_COUNTRY_CODE = "BR"
SEARCH_CURRENCY = "BRL"
SEARCH_PAGE_SIZE = 20


@aliexpress_admin_blueprint.context_processor
def inject_global_data():
    return get_layout_data()


# --------------------------------------------------------------------------
# sessão admin + decorador
# --------------------------------------------------------------------------

def _resolve_ali_session() -> Tuple[Optional[dict], Optional[str]]:
    """Decriptografa o cookie admin. Retorna (payload, None) ou (None, erro).

    erro == "config_missing"  -> env ausente (fail-closed: 500, nunca degrada)
    erro == "expired_invalid" -> cookie ausente/adulterado/expirado
    """
    if not admin_session.is_configured():
        return None, "config_missing"
    payload = admin_session.read_admin_session(
        request.cookies.get(admin_session.COOKIE_NAME)
    )
    if payload is None:
        return None, "expired_invalid"
    return payload, None


def ali_session_required(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        payload, err = _resolve_ali_session()
        if err == "config_missing":
            current_app.logger.error("AliExpress admin session: env ausente (fail-closed)")
            return (
                "Credencial administrativa AliExpress não configurada "
                "(ALIEXPRESS_ADMIN_SESSION_KEY).",
                500,
            )
        if payload is None:
            flash(
                "Sessão AliExpress expirada. Autorize novamente para continuar.",
                "warning",
            )
            return redirect(url_for("aliexpress_admin.index"))
        g.ae_seller_id = str(payload["seller_id"])
        g.ae_access_token = payload["access_token"]
        return func(*args, **kwargs)

    return wrapped


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _aliexpress_client() -> AliExpressClient:
    app_key = (os.environ.get("ALIEXPRESS_APP_KEY") or "").strip()
    app_secret = (os.environ.get("ALIEXPRESS_APP_SECRET") or "").strip()
    if not app_key or not app_secret:
        raise AliExpressConfigError("app_key e app_secret são obrigatórios")
    return AliExpressClient(
        app_key=app_key,
        app_secret=app_secret,
        access_token=g.get("ae_access_token"),
    )


def _parse_amount(field_name: str) -> Optional[Decimal]:
    raw = (request.form.get(field_name) or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    return value


def _norm_currency(value) -> Optional[str]:
    if not value:
        return None
    normalized = str(value).strip().upper()
    return normalized or None


def _equals_decimal(a, b) -> bool:
    """Comparação Decimal tolerante a None (None nunca é igual a valor)."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return Decimal(a) == Decimal(b)


def _load_draft(product_id: str) -> Optional[Product]:
    """Carrega APENAS rascunhos AliExpress editáveis (nunca produtos publicados).

    Um produto publicado (is_active=True / is_draft=False) não pode ser
    carregado nem editado pelas rotas /rascunho/*: a publicação congela os custos
    e preços até uma reimportação explícita (que o importer também bloqueia).
    """
    product = (
        Product.query.options(
            selectinload(Product.supplier_links),
            selectinload(Product.variants),
        )
        .filter_by(
            source="aliexpress",
            external_id=str(product_id),
            is_draft=True,
            is_active=False,
        )
        .first()
    )
    return product


def _active_supplier_product(product: Optional[Product]):
    if product is None:
        return None
    for sp in product.supplier_links or []:
        if getattr(sp, "is_active", False):
            return sp
    return None


def _sanitized_api_error(exc: Exception) -> str:
    """Mensagem de erro sanitizada (nunca body/raw/token/sign)."""
    code = getattr(exc, "code", None)
    sub_code = getattr(exc, "sub_code", None)
    message = getattr(exc, "message", None) or str(exc)
    current_app.logger.error(
        "AliExpress API error: type=%s code=%s sub_code=%s",
        type(exc).__name__,
        code,
        sub_code,
    )
    if code is not None:
        return f"{type(exc).__name__} (code={code} sub_code={sub_code}): {message}"
    return f"{type(exc).__name__}: {message}"


def _parse_optional_int(raw_value, *, minimum=None, maximum=None) -> Optional[int]:
    """Inteiro opcional do formulário; inválido/fora da faixa vira None."""
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if not text:
        return None
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    if isinstance(value, bool):
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _persist_reference_freight_quote(supplier_product, shipping, currency) -> str:
    """Persiste a cotação manual vigente por cenário (PASSO 2, rota manual).

    Grava em ``supplier_reference_freight_quotes`` a prova com validade
    usada pelo checkout agrupado (``SHIPPING_GROUPED_QUOTE_ENABLED=True``):
    custo, prefixo de destino, opção, quantidade 1, moeda BRL, momento e
    validade explícita. Nunca quebra o fluxo legado: qualquer problema
    (cenário ausente, tabela ausente, validação) só gera log + sufixo de
    aviso, e o ``SupplierProduct.shipping_cost`` já salvo é preservado.

    Retorna o sufixo para o flash de sucesso ("" quando nada foi persistido).
    """
    try:
        from loja.services.shipping_quote import normalize_cep
        from loja.services.supplier_freight_quotes import (
            ReferenceQuoteUnavailable,
            store_reference_freight_quote,
        )
        from loja.utils import utcnow
    except Exception:
        current_app.logger.warning("frete admin: quote storage indisponível")
        return " Cotação de referência não persistida (armazenamento indisponível)."

    dest_raw = (request.form.get("freight_dest_cep") or "").strip() or (
        current_app.config.get("SALES_READINESS_DESTINATION_CEP") or ""
    ).strip()
    dest = normalize_cep(dest_raw)
    if dest is None:
        current_app.logger.info("frete admin: reference quote sem cenário de destino")
        return " Cotação de referência não persistida (sem CEP de referência)."

    option_raw = request.form.get("freight_option")
    option = (option_raw.strip() if option_raw else None) or (
        current_app.config.get("SALES_READINESS_SHIPPING_OPTION") or None
    )
    evidence_ref = (request.form.get("freight_evidence_ref") or "").strip() or None
    min_days = _parse_optional_int(request.form.get("freight_min_days"), minimum=0)
    max_days = _parse_optional_int(request.form.get("freight_max_days"), minimum=0)
    if min_days is not None and max_days is not None and max_days < min_days:
        max_days = min_days
    ttl_days = _parse_optional_int(
        request.form.get("freight_ttl_days"), minimum=1, maximum=365
    ) or 30

    now = utcnow()
    if getattr(now, "tzinfo", None) is not None:
        from datetime import timezone

        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    quote = {
        "dest_prefix": dest[:3],
        "option": option,
        "qty": 1,
        "currency": str(currency or "BRL").upper(),
        "cost": shipping,
        "min_days": min_days,
        "max_days": max_days,
        "quoted_at": now,
        "ttl_seconds": int(ttl_days) * 86400,
    }
    try:
        stored = store_reference_freight_quote(
            supplier_product_id=supplier_product.id,
            dest_cep=dest,
            option=option,
            qty=1,
            quote=quote,
            provider_method="admin_manual",
            evidence_ref=evidence_ref,
            now=now,
        )
    except ReferenceQuoteUnavailable as exc:
        current_app.logger.info("frete admin: reference quote rejeitada=%s", exc.code)
        return f" Cotação de referência não persistida ({exc.code})."
    except Exception:
        current_app.logger.warning("frete admin: reference quote falhou", exc_info=True)
        return " Cotação de referência não persistida (erro interno)."
    from datetime import timedelta

    expires_at = stored.get("quoted_at") + timedelta(
        seconds=int(stored.get("ttl_seconds") or 0)
    )
    current_app.logger.info(
        "frete admin: reference quote persistida dest_prefix=%s qty=1",
        dest[:3],
    )
    return f" Cotação de referência vigente até {expires_at.strftime('%d/%m/%Y')}."


# --------------------------------------------------------------------------
# rotas
# --------------------------------------------------------------------------

@aliexpress_admin_blueprint.route("/", methods=["GET"], strict_slashes=False)
@admin_required
def index():
    payload, err = _resolve_ali_session()
    connected = err is None and payload is not None
    session_expired = err == "expired_invalid"
    return render_template(
        "admin/aliexpress/catalogo.html",
        connected=connected,
        session_expired=session_expired,
        results=None,
    )


@aliexpress_admin_blueprint.route("/buscar", methods=["POST"])
@admin_required
@ali_session_required
def buscar():
    keyword = (request.form.get("q") or "").strip()
    if not keyword:
        flash("Informe um termo de busca.", "warning")
        return redirect(url_for("aliexpress_admin.index"))

    try:
        client = _aliexpress_client()
        results = client.search_products(
            keyword=keyword,
            locale=SEARCH_LOCALE,
            country_code=SEARCH_COUNTRY_CODE,
            currency=SEARCH_CURRENCY,
            sort_by="orders,desc",
            page_index=1,
            page_size=SEARCH_PAGE_SIZE,
        )
    except (AliExpressAPIError, AliExpressTransportError, AliExpressConfigError) as exc:
        flash(_sanitized_api_error(exc), "danger")
        return render_template(
            "admin/aliexpress/catalogo.html",
            connected=True,
            session_expired=False,
            results=None,
        )

    return render_template(
        "admin/aliexpress/catalogo.html",
        connected=True,
        session_expired=False,
        results=results,
    )


@aliexpress_admin_blueprint.route("/<product_id>/detalhe", methods=["GET"])
@admin_required
@ali_session_required
def detalhe(product_id: str):
    try:
        client = _aliexpress_client()
        detail = client.get_product(product_id=product_id)
    except (AliExpressAPIError, AliExpressTransportError, AliExpressConfigError) as exc:
        flash(_sanitized_api_error(exc), "danger")
        return redirect(url_for("aliexpress_admin.index"))

    return render_template(
        "admin/aliexpress/detalhe.html",
        detail=detail,
    )


@aliexpress_admin_blueprint.route("/<product_id>/importar", methods=["POST"])
@admin_required
@ali_session_required
def importar(product_id: str):
    try:
        client = _aliexpress_client()
        detail = client.get_product(product_id=product_id)
    except (AliExpressAPIError, AliExpressTransportError, AliExpressConfigError) as exc:
        flash(_sanitized_api_error(exc), "danger")
        return redirect(url_for("aliexpress_admin.detalhe", product_id=product_id))

    try:
        result = import_product_draft(
            detail=detail, seller_id=g.get("ae_seller_id", "")
        )
    except DraftImportError as exc:
        current_app.logger.info("draft import admin: skipped")
        flash(f"Importação como rascunho indisponível ({exc}).", "danger")
        return redirect(url_for("aliexpress_admin.detalhe", product_id=product_id))

    current_app.logger.info(
        "draft import admin: created=%s images=%d variants=c/u/d=%d/%d/%d",
        "yes" if result.created else "no",
        result.images,
        result.variants_created,
        result.variants_updated,
        result.variants_disabled,
    )
    flash("Produto importado como rascunho.", "success")
    return redirect(url_for("aliexpress_admin.rascunho", product_id=product_id))


@aliexpress_admin_blueprint.route("/rascunho/<product_id>", methods=["GET"])
@admin_required
@ali_session_required
def rascunho(product_id: str):
    product = _load_draft(product_id)
    if product is None:
        flash("Rascunho não encontrado. Importe o produto primeiro.", "warning")
        return redirect(url_for("aliexpress_admin.index"))
    supplier_product = _active_supplier_product(product)

    return render_template(
        "admin/aliexpress/rascunho.html",
        product=product,
        supplier_product=supplier_product,
    )


@aliexpress_admin_blueprint.route("/rascunho/<product_id>/frete", methods=["POST"])
@admin_required
@ali_session_required
def rascunho_frete(product_id: str):
    product = _load_draft(product_id)
    supplier_product = _active_supplier_product(product)
    if product is None or supplier_product is None:
        flash("Rascunho/supplier não encontrado.", "warning")
        return redirect(url_for("aliexpress_admin.index"))

    shipping = _parse_amount("shipping_cost")
    currency = _norm_currency(request.form.get("freight_currency"))

    if shipping is None:
        flash("Informe o custo do frete (0,00 só se for realmente gratuito).", "warning")
        return redirect(url_for("aliexpress_admin.rascunho", product_id=product_id))
    if shipping < 0:
        flash("Frete negativo é inválido.", "danger")
        return redirect(url_for("aliexpress_admin.rascunho", product_id=product_id))
    if currency is None:
        flash("Informe a moeda do frete.", "warning")
        return redirect(url_for("aliexpress_admin.rascunho", product_id=product_id))

    supplier_currency = _norm_currency(supplier_product.currency)
    if supplier_currency is None or supplier_currency != currency:
        # Divergência de moeda (fornecedor × frete): sem FX, nunca grava.
        current_app.logger.info("frete admin: currency_mismatch")
        flash("Moeda do frete diverge da moeda do fornecedor (sem conversão).", "danger")
        return redirect(url_for("aliexpress_admin.rascunho", product_id=product_id))

    # Valores ANTERIORES capturados antes de qualquer mutação: usados para
    # detectar mudança material (nunca reprecificar automaticamente).
    prev_shipping = supplier_product.shipping_cost
    prev_currency = supplier_product.freight_currency
    prev_status = supplier_product.freight_status

    supplier_product.freight_status = FREIGHT_AVAILABLE
    supplier_product.shipping_cost = shipping
    supplier_product.freight_currency = currency

    try:
        supplier_product.landed_cost = calculate_landed_cost(supplier_product)
    except PricingBlocked as exc:
        code = exc.args[0] if exc.args else "unknown"
        supplier_product.landed_cost = None
        current_app.logger.info("frete admin: landed blocked reason=%s", code)
        flash(f"Landed cost indisponível ({code}).", "danger")
        return redirect(url_for("aliexpress_admin.rascunho", product_id=product_id))

    # Mudança material de frete invalida a precificação existente: price e
    # target_margin_percent SÃO PRESERVADOS, mas o produto volta a ser STALE
    # (publish bloqueia com pricing_not_ready até reprecificar explicitamente).
    if product.pricing_status == PRICED and (
        _equals_decimal(prev_shipping, shipping) is False
        or (prev_currency or None) != currency
        or (prev_status or None) != FREIGHT_AVAILABLE
    ):
        product.pricing_status = STALE
        product.pricing_error = "freight_changed"
        current_app.logger.info("frete admin: pricing invalidated reason=freight_changed")

    db.session.commit()
    current_app.logger.info("frete admin: status=available")
    quote_note = _persist_reference_freight_quote(
        supplier_product, shipping, currency
    )
    flash("Frete salvo e landed cost recalculado." + quote_note, "success")
    return redirect(url_for("aliexpress_admin.rascunho", product_id=product_id))


@aliexpress_admin_blueprint.route("/rascunho/<product_id>/margem", methods=["POST"])
@admin_required
@ali_session_required
def rascunho_margem(product_id: str):
    product = _load_draft(product_id)
    if product is None:
        flash("Rascunho não encontrado.", "warning")
        return redirect(url_for("aliexpress_admin.index"))

    margin = _parse_amount("target_margin_percent")
    if margin is None:
        flash("Informe a margem alvo (%).", "warning")
        return redirect(url_for("aliexpress_admin.rascunho", product_id=product_id))

    try:
        result = price_dropshipping_product(
            product=product, target_margin_percent=margin
        )
    except PricingError as exc:
        code = exc.args[0] if exc.args else type(exc).__name__
        current_app.logger.info("pricing admin: blocked reason=%s", code)
        flash(f"Precificação bloqueada ({code}).", "danger")
        return redirect(url_for("aliexpress_admin.rascunho", product_id=product_id))

    db.session.commit()
    current_app.logger.info("pricing admin: success")
    flash(
        "Preço calculado: "
        f"{(product.currency or 'BRL')} {result.sale_price} "
        f"com margem {result.target_margin_percent}%.",
        "success",
    )
    return redirect(url_for("aliexpress_admin.rascunho", product_id=product_id))


@aliexpress_admin_blueprint.route("/rascunho/<product_id>/publicar", methods=["POST"])
@admin_required
@ali_session_required
def rascunho_publicar(product_id: str):
    product = _load_draft(product_id)
    if product is None:
        flash("Rascunho não encontrado.", "warning")
        return redirect(url_for("aliexpress_admin.index"))

    try:
        publish_product(product)
    except PublicationBlocked as exc:
        current_app.logger.info("publication admin: blocked")
        flash(
            "Publicação bloqueada: " + ", ".join(str(r) for r in exc.reasons),
            "danger",
        )
        return redirect(url_for("aliexpress_admin.rascunho", product_id=product_id))

    db.session.commit()
    current_app.logger.info("publication admin: success")
    flash("Produto publicado.", "success")
    return redirect(url_for("aliexpress_admin.index"))


@aliexpress_admin_blueprint.route("/encerrar", methods=["POST"])
@admin_required
def encerrar():
    response = redirect(url_for("aliexpress_admin.index"))
    admin_session.clear_admin_session_cookie(response)
    db.session.commit()
    flash("Sessão AliExpress encerrada.", "info")
    return response