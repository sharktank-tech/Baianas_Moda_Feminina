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
  POST /admin/integrations/aliexpress/rascunho/<product_id>/origem  -> origem manual + evidência
  POST /admin/integrations/aliexpress/publicado/<product_id>/origem -> idem (publicado)
  POST /admin/integrations/aliexpress/rascunho/<product_id>/nacional-> seleção de variantes
  POST /admin/integrations/aliexpress/publicado/<product_id>/nacional -> idem (publicado)
  POST /admin/integrations/aliexpress/publicado/<product_id>/sincronizar -> estoque/técnico
  POST /admin/integrations/aliexpress/encerrar                    -> limpa cookie admin

Origem de envio (Etapa 2):
  * Resultado de busca e detalhe NUNCA exibem selo "Brasil": só produtos já
    importados aparecem com etiqueta de origem (chips "Origem não
    verificada" quando não há evidência).
  * POST .../origem grava/apaga a classificação manual COM evidence_ref
    obrigatório, escopo XOR (uma variante ou todas) e transação única.
  * POST .../nacional define quem participa da operação nacional; a
    publicação continua exigindo origem Brasil comprovada por variante.

Sincronização técnica (Etapa 3):
  * POST .../publicado/<id>/sincronizar atualiza SÓ estoque/técnico a partir
    do detalhe já obtido; nunca altera preço, margem, custo, moeda, imagens,
    categoria, publicação, origem de envio ou seleção nacional.
  * Divergência de custo/moeda do fornecedor é sinalizada (sync_error) e
    NUNCA aplicada; nenhum produto é despublicado por esta rota.


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
from typing import Mapping, Optional, Tuple

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
from flask_login import current_user
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
from loja.integrations.aliexpress.product import (
    PARAMETER_NOTE,
    VERDICT_DIFF,
    VERDICT_NO_DIFF,
    VERDICT_UNVIABLE,
    compare_benefit_records,
    fetch_product_record,
    parse_aliexpress_product_ref,
)
from loja.integrations.aliexpress.published_sync import (
    PublishedSyncConflict,
    PublishedSyncError,
    sync_published_product,
)
from loja.modules.models import (
    AliExpressSyncAttempt,
    Product,
    SupplierProduct,
)
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
from loja.services.sales_readiness import REASON_LABELS as READINESS_REASON_LABELS
from loja.services.variant_attributes import variant_attribute_value
from loja.integrations.aliexpress.sku_attr import build_sku_attr
from loja.services.shipping_origin import (
    ShippingOriginError,
    clear_manual_confirmation,
    list_origin_rows,
    national_operation_reasons,
    record_manual_confirmation,
    resolve_product_summary,
    resolve_product_summaries,
    set_national_operation,
    summary_label,
)
from loja.utils.decorators import admin_required

aliexpress_admin_blueprint = Blueprint(
    "aliexpress_admin", __name__, url_prefix=admin_session.ADMIN_CATALOG_PATH
)

SEARCH_LOCALE = "en_US"
SEARCH_COUNTRY_CODE = "BR"
SEARCH_CURRENCY = "BRL"
SEARCH_PAGE_SIZE = 20

#: Filtros administrativos de origem do catálogo importado (Etapa 2).
ORIGIN_FILTERS = ("brazil", "foreign", "unknown", "mixed", "conflict")
#: Teto de produtos importados listados por chamada (painel, sem full scan).
IMPORTED_LIMIT = 50


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
# origem de envio / operação nacional (Etapa 2)
# --------------------------------------------------------------------------

#: Mensagens estáveis de rejeição (o serviço devolve um código, nunca texto).
ORIGIN_ERROR_MESSAGES = {
    "origin_required": "Informe a origem do envio: Brasil ou exterior.",
    "origin_invalid": "Origem inválida: use Brasil ou exterior.",
    "origin_country_invalid": "País inválido: informe 2 letras (ex: BR, CN).",
    "evidence_ref_required": (
        "Informe a referência da evidência. Obrigatória para confirmar origem."
    ),
    "evidence_ref_too_long": (
        "Referência da evidência muito longa (máx. 200 caracteres)."
    ),
    "scope_required": "Informe o escopo: uma variante ou todas.",
    "scope_invalid": "Escopo inválido (variante não pertence a este produto).",
    "variant_without_supplier_sku": (
        "Esta variante não tem SKU do fornecedor: use a cobertura "
        "total (todas as variantes)."
    ),
}

#: Rejeições estáveis da sincronização técnica de produto publicado (Etapa 3).
PUBLISHED_SYNC_MESSAGES = {
    "not_found": "Rascunho/supplier não encontrado.",
    "not_published": (
        "Este produto não está publicado: a sincronização técnica vale para "
        "produto já à venda."
    ),
    "link_mismatch": "Vínculo produto/fornecedor inconsistente: nada foi alterado.",
    "external_id_mismatch": (
        "Os dados recebidos são de outro produto: sincronização cancelada, "
        "nada foi alterado."
    ),
    "missing_product_id": "O fornecedor não devolveu o identificador do produto.",
    "missing_supplier_price": (
        "Nenhum SKU com preço no fornecedor: nada foi alterado."
    ),
    "missing_currency": "Moeda do fornecedor ausente: nada foi alterado.",
    "currency_mismatch": (
        "Mais de uma moeda entre os SKUs: nada foi alterado (sem conversão)."
    ),
    "variant_label_too_long": (
        "Rótulo de variante acima do limite: nada foi alterado."
    ),
}

#: Divergência sinalizada (nunca aplicada) durante a sincronização técnica.
PUBLISHED_SYNC_DIVERGENCE_MESSAGES = {
    "supplier_cost_changed": (
        "Custo do fornecedor divergente do custo gravado: divergência "
        "sinalizada SEM aplicar — preço, margem, precificação e publicação "
        "preservados."
    ),
    "currency_changed": (
        "Moeda do fornecedor divergente da moeda gravada: divergência "
        "sinalizada SEM aplicar — sem FX e sem reprecificação automática."
    ),
}


def _origin_error_message(code: str) -> str:
    return ORIGIN_ERROR_MESSAGES.get(code, "Dados de origem inválidos.")


def _parse_origin_scope(product):
    """Escopo XOR do formulário. Retorna (sku_id, covers_all, erro|None)."""
    raw = (request.form.get("origin_scope") or "").strip()
    if not raw:
        return None, False, "scope_required"
    if raw == "all":
        return None, True, None
    variants = {
        str(variant.id): variant
        for variant in (getattr(product, "variants", None) or [])
    }
    variant = variants.get(raw)
    if variant is None:
        return None, False, "scope_invalid"
    sku_id = getattr(variant, "supplier_sku_id", None)
    if not sku_id:
        return None, False, "variant_without_supplier_sku"
    return str(sku_id), False, None


def _origin_context(product, supplier_product):
    """Contexto de origem das páginas de rascunho/publicado (uma consulta)."""
    summary = resolve_product_summary(product)
    return {
        "origin_summary": summary,
        "origin_display": summary_label(summary.label),
        "origin_reason_labels": [
            READINESS_REASON_LABELS.get(code, code)
            for code in national_operation_reasons(product)
        ],
        "origin_rows": list_origin_rows(
            supplier_product.id if supplier_product is not None else None
        ),
    }


def _back_to(back_endpoint: str, product_id: str):
    return redirect(url_for(back_endpoint, product_id=product_id))


def _apply_origin_update(product, supplier_product, *, back_endpoint,
                         product_id):
    """Grava/apaga a classificação manual de origem (Etapa 2).

    Valida tudo ANTES de mutar; evidência é obrigatória para salvar; a
    operação inteira acontece em uma transação com commit único. Nenhuma
    chamada de fornecedor e nenhum valor presumido.
    """
    action = (request.form.get("origin_action") or "").strip().lower()
    sku_id, covers_all, scope_error = _parse_origin_scope(product)
    if scope_error is not None:
        flash(_origin_error_message(scope_error), "warning")
        return _back_to(back_endpoint, product_id)
    if action not in ("save", "clear"):
        flash("Ação de origem inválida.", "warning")
        return _back_to(back_endpoint, product_id)

    scope_label = "all" if covers_all else "sku"
    try:
        if action == "clear":
            cleared = clear_manual_confirmation(
                supplier_product_id=supplier_product.id,
                sku_id=sku_id,
                covers_all=covers_all,
            )
            db.session.commit()
        else:
            record_manual_confirmation(
                supplier_product_id=supplier_product.id,
                origin=request.form.get("origin_kind"),
                evidence_ref=request.form.get("origin_evidence_ref"),
                sku_id=sku_id,
                covers_all=covers_all,
                origin_country=request.form.get("origin_country"),
                user_id=(
                    current_user.id
                    if getattr(current_user, "is_authenticated", False)
                    else None
                ),
            )
            db.session.commit()
            cleared = None
    except ShippingOriginError as exc:
        db.session.rollback()
        code = exc.args[0] if exc.args else "unknown"
        current_app.logger.info("origem admin: rejected reason=%s", code)
        flash(_origin_error_message(str(code)), "warning")
        return _back_to(back_endpoint, product_id)
    except Exception:
        db.session.rollback()
        current_app.logger.warning("origem admin: falha ao salvar", exc_info=True)
        flash("Erro ao salvar a origem de envio. Nada foi alterado.", "danger")
        return _back_to(back_endpoint, product_id)

    current_app.logger.info(
        "origem admin: action=%s scope=%s", action, scope_label
    )
    if action == "clear":
        flash(
            "Confirmação manual de origem removida."
            if cleared
            else "Nenhuma confirmação manual neste escopo.",
            "info",
        )
    else:
        flash(
            "Origem de envio registrada com evidência. A publicação só "
            "libera a operação nacional com origem Brasil comprovada.",
            "success",
        )
    return _back_to(back_endpoint, product_id)


def _apply_national_update(product, *, back_endpoint, product_id):
    """Seleção explícita de variantes da operação nacional (Etapa 2)."""
    selected_ids = request.form.getlist("national_variant")
    try:
        selected = set_national_operation(product, selected_ids)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.warning(
            "nacional admin: falha ao salvar", exc_info=True
        )
        flash("Erro ao salvar a seleção de operação nacional.", "danger")
        return _back_to(back_endpoint, product_id)
    current_app.logger.info("nacional admin: selected=%d", selected)
    flash(
        "%d variante(s) marcada(s) para a operação nacional. Cada uma "
        "precisa de origem Brasil comprovada para publicar." % selected,
        "success",
    )
    return _back_to(back_endpoint, product_id)


def _imported_origin_rows(origin_filter=None):
    """Produtos AliExpress já importados + resumo honesto de origem."""
    products = (
        Product.query.options(
            selectinload(Product.variants),
            selectinload(Product.supplier_links),
        )
        .filter_by(source="aliexpress")
        .order_by(Product.name.asc(), Product.id.asc())
        .limit(IMPORTED_LIMIT)
        .all()
    )
    summaries = resolve_product_summaries(products)
    rows = []
    for product in products:
        summary = summaries.get(str(product.id))
        if summary is None:
            continue
        if origin_filter and summary.label != origin_filter:
            continue
        variants = [
            variant
            for variant in (product.variants or [])
            if getattr(variant, "is_active", False)
        ]
        selected = [
            variant
            for variant in variants
            if getattr(variant, "national_shipping_operation", False)
        ]
        published = bool(product.is_active) and not product.is_draft
        if published:
            href = url_for(
                "aliexpress_admin.publicado_frete_form",
                product_id=product.external_id or "",
            )
            state = "Publicado"
        else:
            href = url_for(
                "aliexpress_admin.rascunho",
                product_id=product.external_id or "",
            )
            state = "Rascunho"
        rows.append(
            {
                "product": product,
                "label": summary.label,
                "text": summary_label(summary.label),
                "selected": len(selected),
                "total": len(variants),
                "href": href,
                "state": state,
            }
        )
    return rows


def _origin_info_for_ids(external_ids):
    """Só produtos JÁ IMPORTADOS entram aqui: o resto nunca ganha selo."""
    ids = {str(value) for value in external_ids if value}
    if not ids:
        return {}
    products = (
        Product.query.options(
            selectinload(Product.variants),
            selectinload(Product.supplier_links),
        )
        .filter(Product.source == "aliexpress", Product.external_id.in_(ids))
        .all()
    )
    if not products:
        return {}
    summaries = resolve_product_summaries(products)
    info = {}
    for product in products:
        summary = summaries.get(str(product.id))
        if summary is None or not product.external_id:
            continue
        info[str(product.external_id)] = {
            "label": summary.label,
            "text": summary_label(summary.label),
        }
    return info


# --------------------------------------------------------------------------
# rotas
# --------------------------------------------------------------------------

@aliexpress_admin_blueprint.route("/", methods=["GET"], strict_slashes=False)
@admin_required
def index():
    payload, err = _resolve_ali_session()
    connected = err is None and payload is not None
    session_expired = err == "expired_invalid"
    origin_filter = (request.args.get("origem") or "").strip().lower()
    if origin_filter not in ORIGIN_FILTERS:
        origin_filter = None
    return render_template(
        "admin/aliexpress/catalogo.html",
        connected=connected,
        session_expired=session_expired,
        results=None,
        imported=_imported_origin_rows(origin_filter),
        origin_filter=origin_filter,
        origin_filters=ORIGIN_FILTERS,
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
            imported=_imported_origin_rows(),
            origin_filter=None,
            origin_filters=ORIGIN_FILTERS,
        )

    # Resultado de busca NUNCA nasce "nacional": só produtos já importados
    # exibem a etiqueta de origem (com evidência) na listagem.
    origin_info = _origin_info_for_ids(
        (product.item_id or product.external_id)
        for product in (getattr(results, "products", None) or [])
    )
    return render_template(
        "admin/aliexpress/catalogo.html",
        connected=True,
        session_expired=False,
        results=results,
        imported=_imported_origin_rows(),
        origin_filter=None,
        origin_filters=ORIGIN_FILTERS,
        origin_info=origin_info,
    )


@aliexpress_admin_blueprint.route("/buscar-id", methods=["POST"])
@admin_required
@ali_session_required
def buscar_id():
    """Consulta direta por ID ou URL de anúncio (sem passar pela busca).

    A API oficial de busca (`aliexpress.ds.text.search`) não oferece filtro
    por país de despacho: para avaliar um anúncio específico (ex.: com
    envio do Brasil), o caminho comprovado é consultar o detalhe pelo ID
    e conferir variante a variante. Aceita somente ID numérico ou URL
    `/item/` da AliExpress — qualquer outra URL é recusada sem requisição.
    """
    ref = (request.form.get("ref") or "").strip()
    product_id = parse_aliexpress_product_ref(ref)
    if not product_id:
        flash(
            "Informe um ID numérico ou uma URL de anúncio AliExpress "
            "(/item/<id>.html).",
            "warning",
        )
        return redirect(url_for("aliexpress_admin.index"))
    try:
        client = _aliexpress_client()
        detail = client.get_product(product_id=product_id)
    except (AliExpressAPIError, AliExpressTransportError, AliExpressConfigError) as exc:
        flash(_sanitized_api_error(exc), "danger")
        return redirect(url_for("aliexpress_admin.index"))
    if detail is None:
        flash("Produto não encontrado no catálogo do fornecedor.", "warning")
        return redirect(url_for("aliexpress_admin.index"))
    return _render_detalhe(product_id, detail)


def _detalhe_sku_rows(detail):
    """Linhas da tabela de variações: IDs e dimensões por SKU.

    Montado a partir da resposta JÁ consultada (nenhuma chamada adicional,
    nenhuma persistência). Os três identificadores vão em colunas
    SEPARADAS, exatamente como retornados — sem substituir um pelo outro:
      * ``sku_id``: o campo `id` bruto (pode vir como combinação
        `pid:vid;...` no live; ausente vira None e a tela mostra
        "Não fornecido");
      * ``sku_attr``: combinação canônica `pid:vid` construída pelos IDs
        dos atributos (mesma função do envio de pedidos); None quando os
        atributos não trazem identidade numérica completa;
      * ``property_ids``: cada atributo com seu par de IDs
        (`Nome (pid) = Valor (vid)`).
    Cor/tamanho usam o classificador canônico (`variant_attributes`,
    mesma regra da importação).
    """
    rows = []
    for sku in detail.skus or []:
        attributes = sku.attributes or []
        pairs = []
        for attr in attributes:
            if not isinstance(attr, dict) or not attr.get("sku_property_id"):
                continue
            if attr.get("property_value_id"):
                pairs.append(
                    "%s (%s) = %s (%s)"
                    % (
                        attr.get("name"),
                        attr.get("sku_property_id"),
                        attr.get("value"),
                        attr.get("property_value_id"),
                    )
                )
            else:
                pairs.append(
                    "%s (%s) = %s"
                    % (attr.get("name"), attr.get("sku_property_id"), attr.get("value"))
                )
        labels = ", ".join(
            str(attr.get("value"))
            for attr in attributes
            if isinstance(attr, dict) and attr.get("value") not in (None, "")
        )
        price = (
            sku.sale_price if sku.sale_price is not None else sku.original_price
        )
        rows.append(
            {
                "sku_id": sku.sku_id or None,
                "sku_attr": build_sku_attr(attributes),
                "attributes": labels or None,
                "color": variant_attribute_value(attributes, "color"),
                "size": variant_attribute_value(attributes, "size"),
                "property_ids": "; ".join(pairs) or None,
                "stock": sku.available_stock,
                "price": price,
                "currency": sku.currency or detail.currency,
            }
        )
    return rows


def _render_detalhe(product_id: str, detail):
    return render_template(
        "admin/aliexpress/detalhe.html",
        detail=detail,
        # Detalhe é anterior à importação: sem importação não há etiqueta.
        origin_info=_origin_info_for_ids([product_id]),
        sku_rows=_detalhe_sku_rows(detail) if detail is not None else [],
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

    return _render_detalhe(product_id, detail)


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


def _comparar_variant_rows(detail_on, detail_off):
    """Linhas por variante unindo os dois lados da rodada (só leitura).

    `detail_on`/`detail_off` são resumos sanitizados (`summarize`) ou None
    (chamada falhou). Pareamento SOMENTE por `sku_id` idêntico e não nulo:
    SKUs sem ID nunca se agrupam (agrupar sob None sobrescrevia variantes
    e comparava M com G) — cada um vira linha própria, de um lado só, com
    "Pareamento não comprovado" e sem cálculo de diferença. `sku_id`
    ausente continua "Não fornecido" na tela, sem preencher com `sku_attr`
    nem ID inventado. Nenhuma chamada, nenhuma escrita.
    """
    def _skus(detail):
        items = (detail or {}).get("skus") or []
        return [s for s in items if isinstance(s, dict)]

    on_skus = _skus(detail_on)
    off_skus = _skus(detail_off)
    base_on = (detail_on or {}).get("currency")
    base_off = (detail_off or {}).get("currency")

    identified = {}
    for sku in on_skus:
        if sku.get("sku_id") is not None:
            identified.setdefault(sku["sku_id"], {"on": [], "off": []})["on"].append(sku)
    for sku in off_skus:
        if sku.get("sku_id") is not None:
            identified.setdefault(sku["sku_id"], {"on": [], "off": []})["off"].append(sku)

    def _row(sku_id, on, off, *, paired):
        ref = on or off or {}
        attrs = ref.get("attributes") or []
        labels = ", ".join(
            str(a.get("value"))
            for a in attrs
            if isinstance(a, dict) and a.get("value") not in (None, "")
        )
        # Moeda da variante, com fallback explícito para a moeda do produto
        # DA MESMA resposta. Nunca copia do outro lado nem fabrica o BRL
        # pedido na consulta; ausente nos dois níveis vira None ("Não
        # fornecida" na tela).
        currency_on = (on or {}).get("currency") or base_on
        currency_off = (off or {}).get("currency") or base_off
        return {
            "sku_id": sku_id,
            "paired": paired,
            "attributes": labels or None,
            "sku_attr_on": build_sku_attr((on or {}).get("attributes")),
            "sku_attr_off": build_sku_attr((off or {}).get("attributes")),
            "price_on": (on or {}).get("sale_price"),
            "price_off": (off or {}).get("sale_price"),
            "currency_on": currency_on,
            "currency_off": currency_off,
            "prices_comparable": bool(
                currency_on and currency_off and currency_on == currency_off
            ),
            "stock_on": (on or {}).get("available_stock"),
            "stock_off": (off or {}).get("available_stock"),
            "diff": bool(paired) and on != off,
        }

    rows = []
    for sku_id in sorted(identified, key=lambda v: (v is None, v)):
        entry = identified[sku_id]
        for on, off in zip(entry["on"], entry["off"]):
            rows.append(_row(sku_id, on, off, paired=True))
        for on in entry["on"][len(entry["off"]):]:
            rows.append(_row(sku_id, on, None, paired=False))
        for off in entry["off"][len(entry["on"]):]:
            rows.append(_row(sku_id, None, off, paired=False))
    for sku in on_skus:
        if sku.get("sku_id") is None:
            rows.append(_row(None, sku, None, paired=False))
    for sku in off_skus:
        if sku.get("sku_id") is None:
            rows.append(_row(None, None, sku, paired=False))
    return rows


@aliexpress_admin_blueprint.route(
    "/<product_id>/comparar-preco", methods=["POST"]
)
@admin_required
@ali_session_required
def comparar_preco(product_id: str):
    """Uma rodada da comparação on/off do parâmetro experimental (2 chamadas).

    Limites: cada acionamento faz exatamente 2 consultas sequenciais
    (`ds.product.get`, timeout de 10s cada) — bem abaixo do teto de 60s da
    função serverless. Três rodadas = três acionamentos, com ordem alternada
    por paridade (ímpar começa ligado, par começa desligado). Somente
    leitura: nenhum acesso ao banco, nenhuma persistência, sem importar,
    publicar ou comprar.
    """
    try:
        rodada = int((request.form.get("rodada") or "1").strip())
    except (TypeError, ValueError):
        rodada = 0
    if rodada not in (1, 2, 3):
        flash("Rodada inválida: escolha 1, 2 ou 3.", "warning")
        return redirect(
            url_for("aliexpress_admin.detalhe", product_id=product_id)
        )

    first, second = (True, False) if rodada % 2 == 1 else (False, True)
    try:
        client = _aliexpress_client()
    except (AliExpressAPIError, AliExpressTransportError, AliExpressConfigError) as exc:
        flash(_sanitized_api_error(exc), "danger")
        return redirect(
            url_for("aliexpress_admin.detalhe", product_id=product_id)
        )

    label = "comparar-preco-r%d" % rodada
    rec_first = fetch_product_record(
        client, product_id=product_id, label=label,
        remove_personal_benefit=first, round=rodada - 1,
    )
    rec_second = fetch_product_record(
        client, product_id=product_id, label=label,
        remove_personal_benefit=second, round=rodada - 1,
    )
    comparisons = compare_benefit_records([rec_first, rec_second])
    verdict = comparisons[0]["overall"] if comparisons else VERDICT_UNVIABLE
    # `detail_on`/`detail_off` referem-se às FLAGS (on=True), não à ordem.
    on_rec = rec_first if first else rec_second
    off_rec = rec_second if first else rec_first
    rows = _comparar_variant_rows(on_rec.get("detail"), off_rec.get("detail"))

    return render_template(
        "admin/aliexpress/comparar.html",
        product_id=product_id,
        rodada=rodada,
        first_flag=first,
        second_flag=second,
        rec_on=on_rec,
        rec_off=off_rec,
        verdict=verdict,
        verdict_no_diff=VERDICT_NO_DIFF,
        verdict_diff=VERDICT_DIFF,
        verdict_unviable=VERDICT_UNVIABLE,
        parameter_note=PARAMETER_NOTE,
        rows=rows,
    )


def _raw_currency_code(mapping) -> Optional[str]:
    """Extrai `currency_code` bruto sem vazar o resto do payload."""
    if not isinstance(mapping, Mapping):
        return None
    value = mapping.get("currency_code")
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return str(value).strip()


@aliexpress_admin_blueprint.route(
    "/<product_id>/moeda-bruta", methods=["POST"]
)
@admin_required
@ali_session_required
def moeda_bruta(product_id: str):
    """Exibe SÓ os `currency_code` brutos da resposta (só leitura).

    Para fechar a causa-raiz de moeda ausente sem expor o JSON completo
    nem credenciais: base (`ae_item_base_info_dto`) + variante exata
    (`sku_id` do formulário; vazio lista todas). Nenhum acesso ao banco,
    nenhuma persistência, sem importar ou publicar.
    """
    sku_id = (request.form.get("sku_id") or "").strip() or None
    try:
        client = _aliexpress_client()
        detail = client.get_product(product_id=product_id)
    except (AliExpressAPIError, AliExpressTransportError, AliExpressConfigError) as exc:
        flash(_sanitized_api_error(exc), "danger")
        return redirect(
            url_for("aliexpress_admin.detalhe", product_id=product_id)
        )
    if detail is None:
        flash("Produto não encontrado no catálogo do fornecedor.", "warning")
        return redirect(
            url_for("aliexpress_admin.detalhe", product_id=product_id)
        )

    raw = detail.raw if isinstance(detail.raw, dict) else {}
    base_currency = _raw_currency_code(raw.get("ae_item_base_info_dto"))
    variants = []
    for sku in detail.skus or []:
        if sku_id is not None and str(sku.sku_id or "") != sku_id:
            continue
        sku_raw = sku.raw if isinstance(sku.raw, dict) else {}
        variants.append(
            {
                "sku_id": sku.sku_id,
                "currency": _raw_currency_code(sku_raw),
            }
        )

    return render_template(
        "admin/aliexpress/moeda.html",
        product_id=product_id,
        title=getattr(detail, "title", None),
        sku_id=sku_id,
        base_currency=base_currency,
        variants=variants,
    )


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
        **_origin_context(product, supplier_product),
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
        **_origin_context(product, supplier_product),
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


# ── Origem de envio + seleção de operação nacional (Etapa 2) ─────────────

@aliexpress_admin_blueprint.route("/rascunho/<product_id>/origem", methods=["POST"])
@admin_required
@ali_session_required
def rascunho_origem(product_id: str):
    """Classificação manual de origem do envio de um RASCUNHO."""
    product = _load_draft(product_id)
    supplier_product = _active_supplier_product(product)
    if product is None or supplier_product is None:
        flash("Rascunho/supplier não encontrado.", "warning")
        return redirect(url_for("aliexpress_admin.index"))
    return _apply_origin_update(
        product,
        supplier_product,
        back_endpoint="aliexpress_admin.rascunho",
        product_id=product_id,
    )


@aliexpress_admin_blueprint.route("/publicado/<product_id>/origem", methods=["POST"])
@admin_required
@ali_session_required
def publicado_origem(product_id: str):
    """Classificação manual de origem do envio de um PUBLICADO.

    Não mexe em preço, custo, frete, publicação ou estoque: a origem é um
    fato operacional com evidência, não uma decisão de precificação.
    """
    product = _load_published(product_id)
    supplier_product = _active_supplier_product(product)
    if product is None or supplier_product is None:
        flash("Produto publicado não encontrado.", "warning")
        return redirect(url_for("aliexpress_admin.index"))
    return _apply_origin_update(
        product,
        supplier_product,
        back_endpoint="aliexpress_admin.publicado_frete_form",
        product_id=product_id,
    )


@aliexpress_admin_blueprint.route("/rascunho/<product_id>/nacional", methods=["POST"])
@admin_required
@ali_session_required
def rascunho_nacional(product_id: str):
    """Seleção de variantes da operação nacional de um RASCUNHO."""
    product = _load_draft(product_id)
    if product is None:
        flash("Rascunho não encontrado.", "warning")
        return redirect(url_for("aliexpress_admin.index"))
    return _apply_national_update(
        product,
        back_endpoint="aliexpress_admin.rascunho",
        product_id=product_id,
    )


@aliexpress_admin_blueprint.route("/publicado/<product_id>/nacional", methods=["POST"])
@admin_required
@ali_session_required
def publicado_nacional(product_id: str):
    """Seleção de variantes da operação nacional de um PUBLICADO."""
    product = _load_published(product_id)
    if product is None:
        flash("Produto publicado não encontrado.", "warning")
        return redirect(url_for("aliexpress_admin.index"))
    return _apply_national_update(
        product,
        back_endpoint="aliexpress_admin.publicado_frete_form",
        product_id=product_id,
    )


def _sync_attempt(
    *,
    product,
    supplier_product,
    status,
    code=None,
    result=None,
) -> AliExpressSyncAttempt:
    """Monta a auditoria de UMA tentativa (sem payload/token/ID externo)."""
    return AliExpressSyncAttempt(
        product_id=getattr(product, "id", None),
        supplier_product_id=getattr(supplier_product, "id", None),
        status=status,
        code=(str(code)[:64] if code else None),
        divergence=getattr(result, "divergence", None),
        catalog_code=getattr(result, "catalog_code", None),
        variants_updated=int(getattr(result, "variants_updated", 0) or 0),
        variants_deactivated=int(
            getattr(result, "variants_deactivated", 0) or 0
        ),
        variants_created=int(getattr(result, "variants_created", 0) or 0),
        stock_before=getattr(result, "stock_before", None),
        stock_after=getattr(result, "stock_after", None),
    )


def _persist_sync_attempt(**kwargs) -> None:
    """Grava a tentativa em transação própria (após rollback da falha)."""
    try:
        db.session.add(_sync_attempt(**kwargs))
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.warning(
            "sincronização publicado: falha ao gravar tentativa", exc_info=True
        )


@aliexpress_admin_blueprint.route(
    "/publicado/<product_id>/sincronizar", methods=["POST"]
)
@admin_required
@ali_session_required
def publicado_sincronizar(product_id: str):
    """Sincronização TÉCNICA de produto já publicado (Etapa 3).

    Atualiza somente estoque/dados técnicos a partir do detalhe do catálogo.
    Nunca altera decisões comerciais (preço, margem, custo, moeda, imagens,
    categoria, publicação) e nunca despublica; divergência de custo/moeda é
    sinalizada em `sync_error`, invalida a aprovação operacional (preço em
    `stale`) e não é aplicada. Nenhuma chamada de frete, compra ou pedido.

    Toda tentativa — sucesso, recusa, falha ou erro de API — é persistida em
    `aliexpress_sync_attempts`; o flash é só o resumo imediato.
    """
    back_endpoint = "aliexpress_admin.publicado_frete_form"
    product = _load_published(product_id)
    supplier_product = _active_supplier_product(product)
    if product is None or supplier_product is None:
        flash("Produto publicado não encontrado.", "warning")
        return redirect(url_for("aliexpress_admin.index"))

    try:
        client = _aliexpress_client()
        detail = client.get_product(product_id=product_id)
    except (AliExpressAPIError, AliExpressTransportError, AliExpressConfigError) as exc:
        _persist_sync_attempt(
            product=product,
            supplier_product=supplier_product,
            status=AliExpressSyncAttempt.STATUS_API_ERROR,
            code=getattr(exc, "code", None) or "api_error",
        )
        flash(_sanitized_api_error(exc), "danger")
        return _back_to(back_endpoint, product_id)

    if detail is None:
        _persist_sync_attempt(
            product=product,
            supplier_product=supplier_product,
            status=AliExpressSyncAttempt.STATUS_REJECTED,
            code="product_not_found",
        )
        flash("Produto não encontrado no catálogo do fornecedor.", "warning")
        return _back_to(back_endpoint, product_id)

    try:
        result = sync_published_product(
            detail=detail, product=product, supplier_product=supplier_product
        )
        db.session.add(
            _sync_attempt(
                product=product,
                supplier_product=supplier_product,
                status=AliExpressSyncAttempt.STATUS_APPLIED,
                result=result,
            )
        )
        db.session.commit()
    except PublishedSyncError as exc:
        db.session.rollback()
        code = exc.args[0] if exc.args else "unknown"
        current_app.logger.info("sincronização publicado: rejected reason=%s", code)
        _persist_sync_attempt(
            product=product,
            supplier_product=supplier_product,
            status=AliExpressSyncAttempt.STATUS_REJECTED,
            code=code,
        )
        flash(
            PUBLISHED_SYNC_MESSAGES.get(str(code), "Sincronização recusada."),
            "warning",
        )
        return _back_to(back_endpoint, product_id)
    except Exception:
        db.session.rollback()
        current_app.logger.warning(
            "sincronização publicado: falha", exc_info=True
        )
        _persist_sync_attempt(
            product=product,
            supplier_product=supplier_product,
            status=AliExpressSyncAttempt.STATUS_FAILED,
            code="unexpected_error",
        )
        flash("Falha na sincronização. Nenhuma alteração foi aplicada.", "danger")
        return _back_to(back_endpoint, product_id)

    flash(
        "Sincronização técnica concluída: %d variante(s) atualizada(s), "
        "%d desativada(s) por estoque, %d nova(s) opção(ões) inativa(s). "
        "Estoque: %d → %d."
        % (
            result.variants_updated,
            result.variants_deactivated,
            result.variants_created,
            result.stock_before,
            result.stock_after,
        ),
        "success",
    )
    if not result.catalog_complete:
        flash(
            "Resposta do fornecedor sem completude comprovada (%s): os SKUs "
            "ausentes NÃO foram desativados." % (result.catalog_code or "sem motivo"),
            "warning",
        )
    if result.divergence:
        flash(
            PUBLISHED_SYNC_DIVERGENCE_MESSAGES.get(
                result.divergence, "Divergência sinalizada sem aplicar."
            ),
            "warning",
        )
    return _back_to(back_endpoint, product_id)


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
