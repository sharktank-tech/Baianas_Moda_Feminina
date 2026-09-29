"""Catálogo administrativo AliExpress (Fase 7).

 GET  /admin/integrations/aliexpress                             -> status + busca
 POST /admin/integrations/aliexpress/buscar                      -> busca read-only
 GET  /admin/integrations/aliexpress/<product_id>/detalhe        -> detalhe read-only
 POST /admin/integrations/aliexpress/<product_id>/importar       -> import como rascunho
  GET  /admin/integrations/aliexpress/rascunho/<product_id>       -> draft (estado)
  POST /admin/integrations/aliexpress/rascunho/<product_id>/frete -> frete manual
  POST /admin/integrations/aliexpress/publicado/<product_id>/frete -> renovação
       (GET .../publicado/<product_id>/frete -> formulário de renovação)
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
from loja.modules.models import Product, SupplierProduct
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


def _parse_evidence_form(product):
    """Valida os campos de evidência do formulário (ITEM 1.2/1.3).

    Sem defaults silenciosos: referência e validade são OBRIGATÓRIAS, o CEP
    deve ser o exato cotado (8 dígitos), e o escopo de variante é explícito
    ("all" ou UUID de variante do produto). Entrada ausente/inválida é
    REJEITADA com mensagem — nunca substituída por valor presumido.

    Retorna (evidence_dict, None) ou (None, mensagem_de_erro).
    """
    from loja.services.shipping_quote import normalize_cep

    dest = normalize_cep((request.form.get("freight_dest_cep") or "").strip())
    if dest is None:
        return None, "Informe o CEP efetivamente cotado (8 dígitos). Obrigatório."
    dest_end = None
    dest_end_raw = (request.form.get("freight_dest_cep_end") or "").strip()
    if dest_end_raw:
        dest_end = normalize_cep(dest_end_raw)
        if dest_end is None or dest_end < dest:
            return (
                None,
                "Faixa de CEP inválida: o fim deve ter 8 dígitos e ser >= o início.",
            )
    ttl_days = _parse_optional_int(
        request.form.get("freight_ttl_days"), minimum=1, maximum=365
    )
    if ttl_days is None:
        return None, "Informe a validade da cotação em dias (1 a 365). Obrigatória."
    evidence_ref = (request.form.get("freight_evidence_ref") or "").strip()
    if not evidence_ref:
        return (
            None,
            "Informe a referência da evidência (número/link da cotação). Obrigatória.",
        )
    if len(evidence_ref) > 200:
        return None, "Referência da evidência muito longa (máx. 200 caracteres)."
    min_days = _parse_optional_int(
        request.form.get("freight_min_days"), minimum=0, maximum=3650
    )
    if min_days is None and (request.form.get("freight_min_days") or "").strip():
        return None, "Prazo mínimo de entrega inválido."
    max_days = _parse_optional_int(
        request.form.get("freight_max_days"), minimum=0, maximum=3650
    )
    if max_days is None and (request.form.get("freight_max_days") or "").strip():
        return None, "Prazo máximo de entrega inválido."
    if min_days is not None and max_days is not None and max_days < min_days:
        return None, "Prazo máximo menor que o mínimo."
    option_raw = request.form.get("freight_option")
    option = option_raw.strip() if option_raw and option_raw.strip() else None
    if option is not None and len(option) > 120:
        return None, "Opção de envio inválida (máx. 120 caracteres)."
    scope_raw = (request.form.get("freight_variant_scope") or "").strip()
    variant_ids = {
        str(v.id)
        for v in (getattr(product, "variants", None) or [])
        if getattr(v, "id", None) is not None
    }
    covers_all_variants = False
    variant_scope = None
    if scope_raw == "all":
        covers_all_variants = True
    elif scope_raw:
        if scope_raw not in variant_ids:
            return None, "Variante do escopo não pertence a este produto."
        variant_scope = scope_raw
    else:
        return (
            None,
            "Informe o escopo da variante: uma variante específica ou "
            "todas (cobertura multi explícita). Obrigatório.",
        )
    qty_max_raw = (request.form.get("freight_qty_max") or "").strip()
    if not qty_max_raw:
        return None, "Informe o teto de quantidade coberto pela cotação. Obrigatório."
    qty_max = _parse_optional_int(qty_max_raw, minimum=1, maximum=2147483647)
    if qty_max is None:
        return None, "Teto de quantidade inválido (>= 1)."
    return (
        {
            "dest_cep": dest,
            "dest_cep_end": dest_end,
            "option": option,
            "min_days": min_days,
            "max_days": max_days,
            "ttl_days": ttl_days,
            "evidence_ref": evidence_ref,
            "variant_scope": variant_scope,
            "covers_all_variants": covers_all_variants,
            "qty_max": qty_max,
        },
        None,
    )


def _apply_freight_update(product, supplier_product, *, back_endpoint,
                          product_id, published=False):
    """Aplica custo + evidência em UMA transação (ITENS 1, 2, 4).

    Valida tudo antes de mutar (custo/moeda, depois evidência obrigatória);
    em seguida grava custo e cotação com um único commit — qualquer falha
    reverte ambas (ITEM 4a), de modo que o checkout nunca usa custo novo
    com cotação antiga.

    Rascunho: mudança material marca STALE (reprecificar depois).
    Publicado: o CHECK `ck_product_aliexpress_publication_guard` exige
    publicado+`priced`, então mudança material RECALCULA com a MESMA
    margem-alvo na transação (sem margem nova silenciosa; falha reverte
    tudo). Mesmas validações de margem/moeda/landed do rascunho (ITEM 2.2).
    """
    from datetime import timedelta, timezone

    from loja.services.supplier_freight_quotes import (
        ReferenceQuoteUnavailable,
        store_reference_freight_quote,
    )
    from loja.utils import utcnow

    def _back():
        return redirect(url_for(back_endpoint, product_id=product_id))

    # Same lock order as checkout: product then supplier link.
    product = Product.query.filter_by(id=product.id).with_for_update(of=Product).populate_existing().one()
    supplier_product = SupplierProduct.query.filter_by(id=supplier_product.id).with_for_update(of=SupplierProduct).populate_existing().one()
    shipping = _parse_amount("shipping_cost")
    currency = _norm_currency(request.form.get("freight_currency"))

    if shipping is None:
        flash("Informe o custo do frete (0,00 só se for realmente gratuito).", "warning")
        return _back()
    if shipping < 0:
        flash("Frete negativo é inválido.", "danger")
        return _back()
    if currency is None:
        flash("Informe a moeda do frete.", "warning")
        return _back()

    supplier_currency = _norm_currency(supplier_product.currency)
    if supplier_currency is None or supplier_currency != currency:
        # Divergência de moeda (fornecedor × frete): sem FX, nunca grava.
        current_app.logger.info("frete admin: currency_mismatch")
        flash("Moeda do frete diverge da moeda do fornecedor (sem conversão).", "danger")
        return _back()

    evidence, evidence_error = _parse_evidence_form(product)
    if evidence_error is not None:
        current_app.logger.info("frete admin: evidencia rejeitada")
        flash(evidence_error, "warning")
        return _back()

    from loja.services.acquisition_costs import acquisition_components, AcquisitionCostUnavailable
    try:
        components = acquisition_components({
            key: (request.form.get(key) or "").strip().replace(",", ".") or None
            for key in ("supplier_unit_cost", "import_duty", "icms")
        })
    except AcquisitionCostUnavailable:
        flash("Informe custo do produto, imposto de importação e ICMS da cotação. Use zero somente quando comprovado.", "warning")
        return _back()
    if evidence["qty_max"] != 1:
        flash("A cotação completa cobre uma unidade. Informe quantidade máxima 1.", "warning")
        return _back()
    previous_components = (supplier_product.cost, supplier_product.import_duty, supplier_product.icms)
    supplier_product.cost = components["supplier_unit_cost"]
    supplier_product.import_duty = components["import_duty"]
    supplier_product.icms = components["icms"]
    product.cost = components["supplier_unit_cost"]

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
        db.session.rollback()
        current_app.logger.info("frete admin: landed blocked reason=%s", code)
        flash(f"Landed cost indisponível ({code}). Nenhuma alteração foi salva.", "danger")
        return _back()

    # Mudança material de frete invalida a precificação existente: price e
    # target_margin_percent SÃO PRESERVADOS, mas o produto volta a ser STALE
    # (publish bloqueia com pricing_not_ready até reprecificar explicitamente).
    # Publicado não pode ficar STALE (CHECK do banco exige priced): recalcula
    # com a MESMA margem-alvo dentro da transação; falha reverte tudo.
    material_change = (
        previous_components != (supplier_product.cost, supplier_product.import_duty, supplier_product.icms)
        or _equals_decimal(prev_shipping, shipping) is False
        or (prev_currency or None) != currency
        or (prev_status or None) != FREIGHT_AVAILABLE
    )
    new_price = None
    if material_change and published:
        if product.target_margin_percent is None:
            db.session.rollback()
            current_app.logger.info("frete admin: renovação sem margem-alvo")
            flash(
                "Renovação bloqueada: produto sem margem-alvo para "
                "recalcular o preço. Nenhuma alteração foi salva.",
                "danger",
            )
            return _back()
        try:
            result = price_dropshipping_product(
                product=product,
                target_margin_percent=product.target_margin_percent,
            )
            new_price = result.sale_price
        except PricingError as exc:
            code = exc.args[0] if exc.args else type(exc).__name__
            db.session.rollback()
            current_app.logger.info(
                "frete admin: renovação reprecificação bloqueada=%s", code
            )
            flash(
                f"Renovação bloqueada na reprecificação ({code}). "
                "Nenhuma alteração foi salva.",
                "danger",
            )
            return _back()
    elif material_change and product.pricing_status == PRICED:
        product.pricing_status = STALE
        product.pricing_error = "freight_changed"
        current_app.logger.info("frete admin: pricing invalidated reason=freight_changed")

    now = utcnow()
    if getattr(now, "tzinfo", None) is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    quote = {
        **components,
        "dest_prefix": evidence["dest_cep"][:3],
        "option": evidence["option"],
        "qty": 1,
        "currency": currency,
        "cost": shipping,
        "min_days": evidence["min_days"],
        "max_days": evidence["max_days"],
        "quoted_at": now,
        "ttl_seconds": int(evidence["ttl_days"]) * 86400,
        "dest_cep": evidence["dest_cep"],
        "dest_cep_end": evidence["dest_cep_end"],
        "variant_scope": evidence["variant_scope"],
        "covers_all_variants": evidence["covers_all_variants"],
        "qty_max": evidence["qty_max"],
    }
    try:
        stored = store_reference_freight_quote(
            supplier_product_id=supplier_product.id,
            dest_cep=evidence["dest_cep"],
            option=evidence["option"],
            qty=1,
            quote=quote,
            provider_method="admin_manual",
            evidence_ref=evidence["evidence_ref"],
            now=now,
            commit=False,
        )
        db.session.commit()
    except ReferenceQuoteUnavailable as exc:
        db.session.rollback()
        current_app.logger.info("frete admin: evidencia rejeitada=%s", exc.code)
        flash(
            f"Evidência rejeitada ({exc.code}). Nenhuma alteração foi salva.",
            "danger",
        )
        return _back()
    except Exception:
        db.session.rollback()
        current_app.logger.warning("frete admin: falha ao salvar", exc_info=True)
        flash("Erro ao salvar frete e evidência. Nenhuma alteração foi salva.", "danger")
        return _back()

    expires_at = stored.get("quoted_at") + timedelta(
        seconds=int(stored.get("ttl_seconds") or 0)
    )
    current_app.logger.info(
        "frete admin: status=available dest=%s variants=%s",
        evidence["dest_cep"],
        "all" if evidence["covers_all_variants"] else evidence["variant_scope"],
    )
    success_note = (
        "Frete salvo e landed cost recalculado. "
        f"Evidência vigente até {expires_at.strftime('%d/%m/%Y')}."
    )
    if new_price is not None:
        success_note += (
            f" Preço recalculado com a mesma margem "
            f"({product.target_margin_percent}%): {product.currency} {new_price}."
        )
    flash(success_note, "success")
    return _back()


def _load_published(product_id: str) -> Optional[Product]:
    """Carrega produto AliExpress JÁ PUBLICADO para renovação de frete.

    Espelha `_load_draft` com o filtro invertido: só entra produto com
    `is_draft=False` e `is_active=True`. A renovação usa as mesmas regras
    de custo/preço do rascunho (sem atalhos); publicar/despublicar não
    acontece aqui.
    """
    product = (
        Product.query.options(
            selectinload(Product.supplier_links),
            selectinload(Product.variants),
        )
        .filter_by(
            source="aliexpress",
            external_id=str(product_id),
            is_draft=False,
            is_active=True,
        )
        .first()
    )
    return product


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
    return _apply_freight_update(
        product, supplier_product,
        back_endpoint="aliexpress_admin.rascunho", product_id=product_id,
        published=False,
    )


@aliexpress_admin_blueprint.route("/publicado/<product_id>/frete", methods=["GET"])
@admin_required
@ali_session_required
def publicado_frete_form(product_id: str):
    """Formulário de renovação de frete/evidência de produto PUBLICADO.

    ITEM 2: renova a cotação sem exigir despublicação. O POST aplica as
    mesmas regras de custo/preço do rascunho (sem atalhos).
    """
    product = _load_published(product_id)
    supplier_product = _active_supplier_product(product)
    if product is None or supplier_product is None:
        flash("Produto publicado não encontrado.", "warning")
        return redirect(url_for("aliexpress_admin.index"))
    live_evidence = None
    try:
        from loja.services.supplier_freight_quotes import (
            find_valid_reference_quotes,
        )

        ref_dest = (
            current_app.config.get("SALES_READINESS_DESTINATION_CEP") or ""
        ).strip()
        ref_option = (
            current_app.config.get("SALES_READINESS_SHIPPING_OPTION") or None
        )
        if ref_dest:
            snapshot = find_valid_reference_quotes(
                dest_cep=ref_dest, option=ref_option, qty=1
            )
            live_evidence = snapshot.get(str(supplier_product.id))
    except Exception:
        live_evidence = None
    return render_template(
        "admin/aliexpress/renovar_frete.html",
        product=product,
        supplier_product=supplier_product,
        live_evidence=live_evidence,
    )


@aliexpress_admin_blueprint.route("/publicado/<product_id>/frete", methods=["POST"])
@admin_required
@ali_session_required
def publicado_frete(product_id: str):
    product = _load_published(product_id)
    supplier_product = _active_supplier_product(product)
    if product is None or supplier_product is None:
        flash("Produto publicado não encontrado.", "warning")
        return redirect(url_for("aliexpress_admin.index"))
    return _apply_freight_update(
        product, supplier_product,
        back_endpoint="aliexpress_admin.publicado_frete_form",
        product_id=product_id,
        published=True,
    )


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
