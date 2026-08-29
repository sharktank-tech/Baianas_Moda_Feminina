"""Rotas OAuth AliExpress (blueprint dedicado, isolado do core Flask).

 GET /integrations/aliexpress/authorize  -> gera state, redireciona p/ AliExpress
 GET /integrations/aliexpress/callback   -> valida state, troca code, faz DS smoke

Regras de segurança:
  - state obrigatório, comparado com hmac.compare_digest, consumido após uso.
  - NUNCA mostra/exibe access_token, refresh_token, app_secret, code ou sign.
  - Token usado SOMENTE em memória para o smoke read-only; não gravado em
    cookie/session/.env. Sem refresh automático.
"""

from __future__ import annotations

import hmac
import os
import time
from functools import wraps
from typing import Any, Callable, Optional

from flask import Blueprint, current_app, redirect, request, session

from loja.integrations.aliexpress import AliExpressClient, DEFAULT_BASE_URL
from loja.integrations.aliexpress.errors import (
    AliExpressAPIError,
    AliExpressAuthError,
    AliExpressConfigError,
    AliExpressRateLimitError,
    AliExpressTransportError,
)
from loja.integrations.aliexpress.oauth import (
    DEFAULT_OAUTH_API_BASE_URL,
    DEFAULT_OAUTH_AUTHORIZE_URL,
    build_authorization_url,
    exchange_authorization_code,
    generate_oauth_state,
)
from loja.utils.decorators import admin_required

STATE_KEY = "aliexpress_oauth_state"
STATE_MAX_AGE = 600  # 10 minutos
_url_prefix = "/integrations/aliexpress"

aliexpress_blueprint = Blueprint("aliexpress", __name__, url_prefix=_url_prefix)


def _env(name: str, default: Optional[str]) -> Optional[str]:
    return os.environ.get(name, default)


def _authorize_url_default() -> str:
    return _env("ALIEXPRESS_OAUTH_AUTHORIZE_URL", DEFAULT_OAUTH_AUTHORIZE_URL)


def _api_base_default() -> str:
    return _env("ALIEXPRESS_OAUTH_API_BASE_URL", DEFAULT_OAUTH_API_BASE_URL)


def get_aliexpress_callback_url() -> str:
    """Resolve a URL de callback OAuth sem depender de ALIEXPRESS_CALLBACK_URL.

    Precedência:
      1. ALIEXPRESS_CALLBACK_URL (explícito, sempre vence)
      2. VERCEL_ENV == "preview"  -> VERCEL_BRANCH_URL (host estável da branch)
      3. VERCEL_ENV == "production" -> VERCEL_PROJECT_PRODUCTION_URL
      4. fallback local/controlado -> VERCEL_PROJECT_PRODUCTION_URL (se houver)
      5. erro (AliExpressConfigError)

    Em produção NUNCA usamos uma URL de branch. VERCEL_BRANCH_URL não inclui o
    esquema https:// (doc Vercel); nós prefixamos.
    """
    explicit = (os.environ.get("ALIEXPRESS_CALLBACK_URL") or "").strip()
    if explicit:
        return explicit

    vercel_env = (
        os.environ.get("VERCEL_ENV") or os.environ.get("VERCEL_TARGET_ENV") or ""
    ).strip().lower()
    branch_url = (os.environ.get("VERCEL_BRANCH_URL") or "").strip()
    production_url = (os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or "").strip()

    if vercel_env == "preview":
        if branch_url:
            return f"https://{branch_url}/integrations/aliexpress/callback"
    elif vercel_env == "production":
        if production_url:
            return f"https://{production_url}/integrations/aliexpress/callback"
    else:
        # Ambiente local/controlado: usa a URL de produção se disponível.
        if production_url:
            return f"https://{production_url}/integrations/aliexpress/callback"

    raise AliExpressConfigError(
        "Callback OAuth AliExpress não configurado."
    )


def canonicalize_vercel_branch(func: Callable) -> Callable:
    """Redireciona para a branch URL estável ANTES de qualquer autenticação.

    Em Preview Vercel, o cookie de sessão (e o state OAuth) deve pertencer a um
    hostname estável. Se acessado pela deployment URL (outro host), redireciona
    para a branch URL antes de acionar admin_required / criar state. Não copia
    query strings sensíveis.
    """

    @wraps(func)
    def wrapped(*args, **kwargs):
        branch_host = (os.environ.get("VERCEL_BRANCH_URL") or "").strip()
        if (
            os.environ.get("VERCEL_ENV") == "preview"
            and branch_host
            and request.host != branch_host
        ):
            return redirect(
                f"https://{branch_host}/integrations/aliexpress/authorize"
            )
        return func(*args, **kwargs)

    return wrapped


@aliexpress_blueprint.route("/authorize", methods=["GET"])
@canonicalize_vercel_branch
@admin_required
def authorize():
    app_key = (os.environ.get("ALIEXPRESS_APP_KEY") or "").strip()
    if not app_key:
        return "AliExpress OAuth não configurado (ALIEXPRESS_APP_KEY ausente).", 500

    try:
        callback_url = get_aliexpress_callback_url()
    except AliExpressConfigError:
        return "AliExpress OAuth não configurado (callback ausente).", 500

    # Validação sanitizada: presença/comprimento apenas. Nunca expõe o valor.
    current_app.logger.info(
        "AliExpress OAuth app_key configurado: yes, length=%d", len(app_key)
    )

    state = generate_oauth_state()
    # state: geração criptográfica + uso único + expiração curta + consumo.
    session[STATE_KEY] = {"state": state, "ts": time.time()}
    url = build_authorization_url(
        client_id=app_key,
        redirect_uri=callback_url,
        state=state,
        authorize_url=_authorize_url_default(),
    )

    return redirect(url)


@aliexpress_blueprint.route("/callback", methods=["GET"])
def callback():
    code = request.args.get("code")
    state = request.args.get("state")
    # Consome imediatamente (uso único); qualquer falha abaixo rejeita.
    stored = session.pop(STATE_KEY, None)

    if not isinstance(stored, dict):
        return "Falha de validação de estado OAuth (possível CSRF).", 400
    if not state or not hmac.compare_digest(state, stored.get("state", "")):
        return "Falha de validação de estado OAuth (possível CSRF).", 400
    if time.time() - float(stored.get("ts", 0)) > STATE_MAX_AGE:
        return "Estado OAuth expirado.", 400
    if not code:
        return "Código de autorização ausente.", 400

    app_key = os.environ.get("ALIEXPRESS_APP_KEY")
    app_secret = os.environ.get("ALIEXPRESS_APP_SECRET")
    if not app_key or not app_secret:
        return "AliExpress OAuth não configurado.", 500

    # 1) Troca do code por access_token (Open Platform).
    try:
        token = exchange_authorization_code(
            app_key, app_secret, code, base_url=_api_base_default()
        )
    except (AliExpressAuthError, AliExpressTransportError) as exc:
        return _render_oauth_failure(exc)

    # 2) DS smoke read-only usando o token SOMENTE em memória.
    client = AliExpressClient(app_key=app_key, app_secret=app_secret, access_token=token.access_token)
    try:
        result = client.search_products(
            keyword="t shirt",
            locale="en_US",
            country_code="BR",
            currency="BRL",
            sort_by="orders,desc",
            page_index=1,
            page_size=5,
        )
    except (AliExpressAPIError, AliExpressTransportError) as exc:
        return _render_ds_failure(token, exc)

    # 3) Product detail read-only (Fase 3) com o MESMO token em memória.
    #    Usa o primeiro product_id retornado pela busca.
    first_id = None
    if result.products:
        first_id = result.products[0].item_id or result.products[0].external_id
    product_detail = None
    product_error = None
    if first_id:
        try:
            product_detail = client.get_product(product_id=first_id)
        except (AliExpressAPIError, AliExpressTransportError) as exc:
            product_error = exc

    # 4) Freight query (Fase 4) read-only com o MESMO token em memória.
    #    Sonda candidatos (produtos da busca x SKUs válidos) e para no primeiro
    #    com opções. 501/505 = indisponibilidade do candidato (tenta o próximo);
    #    demais erros (507, auth, assinatura, MissingParameter, ApiCallLimit,
    #    transporte) abortam o smoke normalmente.
    freight_result = None
    freight_error = None
    freight_unavailable = None
    freight_candidates = 0
    unavail = {"attempted": 0, "code_501": 0, "code_505": 0, "other": 0}

    candidates = _iter_freight_candidates(
        client, result.products or [], max_products=5, max_skus_per_product=3
    )
    for product_id, sku_id in candidates:
        if freight_candidates >= MAX_FREIGHT_CALLS:
            break
        try:
            fret = client.query_freight(
                product_id=product_id,
                selected_sku_id=sku_id,
                ship_to_country="BR",
                currency="BRL",
                quantity=1,
            )
        except (AliExpressAPIError, AliExpressTransportError) as exc:
            freight_candidates += 1
            if isinstance(
                exc, (AliExpressAuthError, AliExpressRateLimitError, AliExpressTransportError)
            ):
                # Erro estrutural: aborta (não é "tentar próximo").
                freight_error = exc
                break
            code = getattr(exc, "code", None)
            if _is_unavailable_code(code):
                if code in (501, "501"):
                    unavail["code_501"] += 1
                elif code in (505, "505"):
                    unavail["code_505"] += 1
                else:
                    unavail["other"] += 1
                _freight_backoff()
                continue
            unavail["other"] += 1
            freight_error = exc
            break
        freight_candidates += 1
        if fret.options:
            freight_result = fret
            break
        # Sucesso sem opções: tenta o próximo candidato.
        _freight_backoff()

    if freight_result is None and freight_error is None:
        unavail["attempted"] = freight_candidates
        freight_unavailable = unavail
        # Diagnóstico seguro (sem IDs/token/sign/request body) quando tudo 501/505.
        current_app.logger.info(
            "freight candidate summary: attempted=%d code_501=%d code_505=%d other=%d "
            "province_provided=no city_provided=no",
            unavail["attempted"],
            unavail["code_501"],
            unavail["code_505"],
            unavail["other"],
        )

    return _render_success(
        token,
        result,
        product_detail=product_detail,
        product_error=product_error,
        freight_result=freight_result,
        freight_error=freight_error,
        freight_unavailable=freight_unavailable,
        freight_candidates=freight_candidates,
    )


# Limite de chamadas de frete no smoke (evita rate limit no endpoint).
MAX_FREIGHT_CALLS = 8


def _is_unavailable_code(code: Any) -> bool:
    """501/505 = indisponibilidade do candidato (tentar próximo), não erro fatal."""
    return code in (501, 505, "501", "505")


def _freight_backoff() -> None:
    """Backoff simples entre chamadas adicionais (somente no Preview Vercel)."""
    if os.environ.get("VERCEL_ENV") == "preview":
        time.sleep(1.1)


def _iter_freight_candidates(
    client: Any,
    products: Any,
    *,
    max_products: int = 5,
    max_skus_per_product: int = 3,
) -> Any:
    """Gera candidatos (product_id, sku_id) elegíveis, com limites.

    Para cada produto da busca (até max_products), busca o detalhe e emite até
    max_skus_per_product SKUs válidos (sku_id presente e estoque não-zero/vazio).
    Falhas de get_product pulam para o próximo produto.
    """
    if not isinstance(products, (list, tuple)):
        return
    count_p = 0
    for prod in products:
        if count_p >= max_products:
            return
        product_id = getattr(prod, "item_id", None) or getattr(prod, "external_id", None)
        if not product_id:
            continue
        try:
            detail = client.get_product(product_id=product_id)
        except (AliExpressAPIError, AliExpressTransportError):
            count_p += 1
            continue
        count_p += 1
        skus = getattr(detail, "skus", None) or []
        valid = 0
        for sku in skus:
            if valid >= max_skus_per_product:
                break
            sku_id = getattr(sku, "sku_id", None)
            if not sku_id:
                continue
            stock = getattr(sku, "available_stock", None)
            if stock is not None and stock <= 0:
                continue
            yield product_id, sku_id
            valid += 1


# --------------------------------------------------------------------------
# Renderização sanitizada (nunca tokens/code/sign)
# --------------------------------------------------------------------------
def _render_success(
    token: Any,
    result: Any,
    *,
    product_detail: Any = None,
    product_error: Any = None,
    freight_result: Any = None,
    freight_error: Any = None,
    freight_unavailable: Any = None,
    freight_candidates: int = 0,
) -> str:
    lines = [
        "AliExpress autorizado com sucesso",
        f"seller_id: {token.seller_id}",
        "access token: obtido",
        f"expires_in: {token.expires_in}",
        "",
        "OAuth: success",
        "DS search: success",
        f"gateway: {_ds_gateway()}",
        "method: aliexpress.ds.text.search",
        "sign_method: sha256",
        f"produtos recebidos: {len(result.products)}",
    ]

    # Seção product.get (Fase 3) — sanitizada (sem token/code/sign).
    if product_error is not None:
        code = getattr(product_error, "code", None)
        sub_code = getattr(product_error, "sub_code", None)
        raw = getattr(product_error, "raw", None)
        request_id = _find_request_id(raw)
        current_app.logger.error(
            "AliExpress DS product.get falhou: type=%s message=%s code=%s sub_code=%s request_id=%s",
            type(product_error).__name__,
            getattr(product_error, "message", str(product_error)),
            code,
            sub_code,
            request_id,
        )
        lines += [
            "",
            "DS product.get: failure",
            f"code: {code}",
            f"sub_code: {sub_code}",
            f"request_id: {request_id}",
        ]
    elif product_detail is not None:
        skus = product_detail.skus or []
        imgs = product_detail.images or []
        has_price = (
            product_detail.sale_price is not None
            or product_detail.original_price is not None
            or any(
                (sku.sale_price is not None or sku.original_price is not None)
                for sku in skus
            )
        )
        has_stock = product_detail.available_stock is not None or any(
            sku.available_stock is not None for sku in skus
        )
        lines += [
            "",
            "DS product.get: success",
            f"product_id: {'presente' if product_detail.product_id else 'ausente'}",
            f"SKUs recebidos: {len(skus)}",
            f"imagens recebidos: {len(imgs)}",
            f"preço: {'presente' if has_price else 'ausente'}",
            f"estoque: {'presente' if has_stock else 'ausente'}",
        ]

    # Seção freight.query (Fase 4) — sanitizada (sem token/code/sign).
    if freight_error is not None:
        code = getattr(freight_error, "code", None)
        sub_code = getattr(freight_error, "sub_code", None)
        raw = getattr(freight_error, "raw", None)
        request_id = _find_request_id(raw)
        msg = getattr(freight_error, "message", str(freight_error))
        current_app.logger.error(
            "AliExpress DS freight.query falhou: type=%s message=%s code=%s sub_code=%s request_id=%s",
            type(freight_error).__name__,
            msg,
            code,
            sub_code,
            request_id,
        )
        lines += [
            "",
            "DS freight.query: failure",
            f"code: {code}",
            f"sub_code: {sub_code}",
            f"request_id: {request_id}",
            f"mensagem: {msg}",
        ]
    elif freight_unavailable is not None:
        s = freight_unavailable
        lines += [
            "",
            "DS freight.query: unavailable",
            f"candidatos testados: {s['attempted']}",
            f"code 501: {s['code_501']}",
            f"code 505: {s['code_505']}",
            f"outros erros: {s['other']}",
        ]
    elif freight_result is not None:
        options = freight_result.options or []
        has_fee = freight_result.min_fee is not None
        has_currency = freight_result.currency is not None
        has_days = freight_result.min_delivery_days is not None
        lines += [
            "",
            "DS freight.query: success",
            f"candidatos testados: {freight_candidates}",
            f"opções de frete: {len(options)}",
            f"frete mínimo: {'presente' if has_fee else 'ausente'}",
            f"moeda: {'presente' if has_currency else 'ausente'}",
            f"prazo: {'presente' if has_days else 'ausente'}",
        ]
    else:
        lines += ["", "DS freight.query: skipped (sem SKU elegível)"]

    return "<br>\n".join(lines)


def _render_oauth_failure(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    sub_code = getattr(exc, "sub_code", None)
    raw = getattr(exc, "raw", None)
    request_id = raw.get("request_id") if isinstance(raw, dict) else None
    # Log sanitizado: apenas a mensagem (sem code OAuth/state/token/sign).
    current_app.logger.error("AliExpress OAuth token exchange falhou: %s", getattr(exc, "message", str(exc)))
    lines = [
        "AliExpress OAuth falhou",
        "OAuth: failure",
        f"code: {code}",
        f"sub_code: {sub_code}",
        f"request_id: {request_id}",
        "DS smoke: skipped",
        "",
        "Detalhe sanitizado: verifique code/sub_code/request_id no log da aplicação.",
    ]
    return "<br>\n".join(lines), 200


def _render_ds_failure(token: Any, exc: Exception) -> str:
    code = getattr(exc, "code", None)
    sub_code = getattr(exc, "sub_code", None)
    raw = getattr(exc, "raw", None)
    request_id = _find_request_id(raw)
    # Log sanitizado: apenas tipo/mensagem/code/sub_code/request_id. Nunca
    # token, sign, secret, params ou body.
    current_app.logger.error(
        "AliExpress DS smoke falhou: type=%s message=%s code=%s sub_code=%s request_id=%s",
        type(exc).__name__,
        getattr(exc, "message", str(exc)),
        code,
        sub_code,
        request_id,
    )
    lines = [
        "AliExpress autorizado, mas o smoke DS falhou",
        "OAuth: success",
        "DS smoke: failure",
        f"code: {code}",
        f"sub_code: {sub_code}",
        f"request_id: {request_id}",
        f"gateway: {_ds_gateway()}",
        "method: aliexpress.ds.text.search",
        "",
        "Detalhe sanitizado: erro registrado no log da aplicação (sem dados sensíveis).",
    ]
    return "<br>\n".join(lines), 200


def _ds_gateway() -> str:
    return DEFAULT_BASE_URL


def _find_request_id(obj: object) -> Optional[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() == "request_id":
                return str(v)
            rid = _find_request_id(v)
            if rid:
                return rid
    if isinstance(obj, list):
        for item in obj:
            rid = _find_request_id(item)
            if rid:
                return rid
    return None
