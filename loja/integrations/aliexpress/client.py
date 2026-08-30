"""Cliente HTTP da AliExpress Open Platform (gateway migrado `/sync`).

O cliente é puro (sem Flask/SQLAlchemy). O transporte é injetável para testes.
As credenciais NUNCA são logadas; o `sign` é computado localmente.

Protocolo migrado (endpoint `/sync`):
  - URL: https://api-sg.aliexpress.com/sync
  - Parâmetros públicos: method, app_key, session, timestamp, format, v,
    simplify, sign_method, sign
  - timestamp: Unix epoch em milissegundos (13 dígitos)
  - assinatura: HMAC-SHA256 (uppercase), material = params ordenados (sem
    prefixo de path), excluindo apenas `sign`
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional

from .auth import AliExpressCredentials
from .errors import (
    AliExpressAPIError,
    AliExpressAuthError,
    AliExpressConfigError,
    AliExpressRateLimitError,
    AliExpressTransportError,
    redact,
)
from .sign import sign_sync
from .transport import RequestsTransport, Transport, TransportError, TransportTimeout

DEFAULT_BASE_URL = "https://api-sg.aliexpress.com/sync"
DEFAULT_TIMEOUT = 10.0
DEFAULT_SIGN_METHOD = "sha256"
DEFAULT_SESSION_PARAM = "session"

_RATE_LIMIT_CODES = {"555"}
_RATE_LIMIT_SUBSTRINGS = ("rate limit", "too many requests", "request limit")


class AliExpressClient:
    """Cliente mínimo para o gateway migrado `/sync` da AliExpress Open Platform.

    Parâmetros
    ----------
    app_key, app_secret: obrigatórios.
    access_token: opcional; enviado como parâmetro `session`.
    base_url, timeout, sign_method, session_param: configuráveis/testáveis.
    transport: injetável; padrão `RequestsTransport`. Nunca nulo em runtime.
    """

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        access_token: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        sign_method: str = DEFAULT_SIGN_METHOD,
        session_param: str = DEFAULT_SESSION_PARAM,
        transport: Optional[Transport] = None,
    ) -> None:
        if not app_key or not app_secret:
            raise AliExpressConfigError("app_key e app_secret são obrigatórios")

        self._creds = AliExpressCredentials(
            app_key=app_key, app_secret=app_secret, access_token=access_token
        )
        self.base_url = base_url
        self.timeout = timeout
        self.sign_method = sign_method
        self.session_param = session_param
        self._transport = transport or RequestsTransport(base_url, timeout=timeout)

    # ----- helpers -------------------------------------------------------
    @property
    def credentials(self) -> AliExpressCredentials:
        return self._creds

    def with_token(self, access_token: str) -> "AliExpressClient":
        """Retorna uma cópia do cliente com outro access_token (sem mutar o original)."""
        new_creds = replace(self._creds, access_token=access_token)
        new = AliExpressClient(
            app_key=new_creds.app_key,
            app_secret=new_creds.app_secret,
            access_token=access_token,
            base_url=self.base_url,
            timeout=self.timeout,
            sign_method=self.sign_method,
            session_param=self.session_param,
            transport=self._transport,
        )
        return new

    def _now_timestamp(self) -> str:
        # Open Platform (gateway /sync) exige Unix epoch em milissegundos (13 dígitos).
        return str(int(time.time() * 1000))

    @staticmethod
    def _coerce(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (dict, list)):
            return json.dumps(value, separators=(",", ":"))
        return str(value)

    def _build_params(self, method: str, **business: Any) -> dict[str, str]:
        params: dict[str, str] = {
            "method": method,
            "app_key": self._creds.app_key,
            "timestamp": self._now_timestamp(),
            "format": "json",
            "v": "2.0",
            "simplify": "true",
            "sign_method": self.sign_method,
        }
        if self._creds.access_token:
            params[self.session_param] = self._creds.access_token

        for key, val in business.items():
            if val is None:
                continue
            params[key] = self._coerce(val)

        # Assinatura /sync: HMAC-SHA256 (uppercase) sobre todos os parâmetros
        # (públicos + negócio), excluindo apenas `sign`. Não há prefixo de path.
        params["sign"] = sign_sync(
            {k: v for k, v in params.items() if k != "sign"},
            self._creds.app_secret,
        )
        return params

    # ----- chamada -------------------------------------------------------
    def call(self, method: str, **business: Any) -> Any:
        """Executa uma chamada à API e retorna o payload de resposta decodificado.

        Levanta AliExpressAPIError em erro de negócio e AliExpressTransportError
        em falha de rede/timeout. Nunca retorna None silenciosamente.
        """
        params = self._build_params(method, **business)
        try:
            raw = self._transport.post(params)
        except TransportTimeout as exc:
            raise AliExpressTransportError(f"timeout: {exc}") from exc
        except TransportError as exc:
            raise AliExpressTransportError(str(exc)) from exc

        return self._parse_response(raw, method=method)

    @staticmethod
    def _parse_response(raw: str, *, method: str) -> Any:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AliExpressAPIError(
                "resposta não é JSON válido", raw=raw[:500]
            ) from exc

        if not isinstance(data, dict):
            raise AliExpressAPIError("resposta inesperada (não é objeto)", raw=data)

        if "error_response" in data:
            err = data["error_response"] or {}
            code = err.get("code")
            sub_code = err.get("sub_code")
            msg = err.get("msg") or err.get("sub_msg") or "erro desconhecido"
            return AliExpressClient._raise_for_error(msg, code=code, sub_code=sub_code, raw=data)

        # Resposta por método: chave "<method>_response" (ou a variante live com
        # underscores, ex.: "aliexpress_ds_text_search_response").
        resp_key = f"{method}_response"
        alt_key = method.replace(".", "_") + "_response"
        payload = data.get(resp_key, data.get(alt_key, data))
        if isinstance(payload, dict):
            code = payload.get("code")
            if code is not None and not AliExpressClient._is_success_code(code):
                sub_code = payload.get("sub_code")
                msg = payload.get("msg") or "erro de API"
                return AliExpressClient._raise_for_error(
                    msg, code=code, sub_code=sub_code, raw=data
                )
        return payload

    @staticmethod
    def _is_success_code(code: Any) -> bool:
        """Códigos zero-like (0, "0", "00", "000", None) são sucesso.

        Códigos realmente não-zero (ex.: 20020, "400") continuam sendo erro.
        """
        if code is None:
            return True
        value = str(code).strip()
        return bool(value) and value.isdigit() and int(value) == 0

    @staticmethod
    def _raise_for_error(msg: str, *, code: Any, sub_code: Any, raw: Any) -> Any:
        code_str = str(code) if code is not None else ""
        text = f"{msg} (code={code}, sub_code={sub_code})"
        if code_str in _RATE_LIMIT_CODES or any(
            s in msg.lower() for s in _RATE_LIMIT_SUBSTRINGS
        ):
            raise AliExpressRateLimitError(text, code=code, sub_code=sub_code, raw=raw)
        if code_str in ("401", "403", "40010", "27"):
            raise AliExpressAuthError(text, code=code, sub_code=sub_code, raw=raw)
        raise AliExpressAPIError(text, code=code, sub_code=sub_code, raw=raw)

    def __repr__(self) -> str:
        return (
            f"AliExpressClient(base_url={self.base_url!r}, "
            f"app_key=***, access_token={'***' if self._creds.access_token else None}, "
            f"sign_method={self.sign_method!r})"
        )

    # ----- busca (Fase 2) -------------------------------------------------
    def search_products(
        self,
        *,
        keyword: Optional[str] = None,
        locale: str = "en_US",
        country_code: str = "BR",
        currency: str = "BRL",
        category_id: Optional[str] = None,
        sort_by: str = "orders,desc",
        page_index: int = 1,
        page_size: int = 20,
        search_extend: Optional[str] = None,
        selection_name: Optional[str] = None,
    ) -> "AliExpressSearchResult":
        """Busca read-only aliexpress.ds.text.search (retorna resultado normalizado).

        Não persiste nada. Em caso de erro TOP/API, levanta a hierarquia existente
        (AliExpressAuthError / AliExpressRateLimitError / AliExpressAPIError /
        AliExpressTransportError). Lista vazia significa "busca válida, 0 produtos".
        """
        from .search import build_search_params, normalize_search_response

        business = build_search_params(
            keyword=keyword,
            locale=locale,
            country_code=country_code,
            currency=currency,
            category_id=category_id,
            sort_by=sort_by,
            page_index=page_index,
            page_size=page_size,
            search_extend=search_extend,
            selection_name=selection_name,
        )
        payload = self.call("aliexpress.ds.text.search", **business)
        return normalize_search_response(payload, method="aliexpress.ds.text.search")

    # ----- detalhe do produto (Fase 3, read-only) ---------------------------
    def get_product(
        self,
        *,
        product_id: str,
        ship_to_country: str = "BR",
        target_currency: str = "BRL",
        target_language: str = "pt",
        remove_personal_benefit: bool = True,
        biz_model: Optional[str] = None,
        province: Optional[str] = None,
        city: Optional[str] = None,
    ) -> "AliExpressProductDetail":
        """Busca read-only aliexpress.ds.product.get (retorna detalhe normalizado).

        Não persiste nada. Usa o mesmo gateway /sync, sign_method=sha256 e o
        access_token como `session`. Lista de SKUs/atributos vazia significa
        "produto válido, 0 SKUs/atributos". Toll/impostos preservados em `raw`.
        """
        from .product import build_product_detail_params, normalize_product_detail

        business = build_product_detail_params(
            product_id=product_id,
            ship_to_country=ship_to_country,
            target_currency=target_currency,
            target_language=target_language,
            remove_personal_benefit=remove_personal_benefit,
            biz_model=biz_model,
            province=province,
            city=city,
        )
        payload = self.call("aliexpress.ds.product.get", **business)
        return normalize_product_detail(payload, method="aliexpress.ds.product.get")

    # ----- frete (Fase 4, read-only) ----------------------------------------
    def query_freight(
        self,
        *,
        product_id: str,
        selected_sku_id: str,
        ship_to_country: str = "BR",
        currency: str = "BRL",
        quantity: int = 1,
        province_code: Optional[str] = None,
        city_code: Optional[str] = None,
        language: str = "en_US",
        locale: Optional[str] = None,
    ) -> "AliExpressFreightResult":
        """Busca read-only aliexpress.ds.freight.query (retorna frete normalizado).

        Não persiste nada. Usa o mesmo gateway /sync, sign_method=sha256 e o
        access_token como `session`. O parâmetro de negócio `queryDeliveryReq`
        é um JSON compacto e participa da assinatura.
        """
        from .freight import build_freight_params, normalize_freight_response

        business = build_freight_params(
            product_id=product_id,
            selected_sku_id=selected_sku_id,
            ship_to_country=ship_to_country,
            currency=currency,
            quantity=quantity,
            province_code=province_code,
            city_code=city_code,
            language=language,
            locale=locale,
        )
        payload = self.call("aliexpress.ds.freight.query", **business)
        return normalize_freight_response(payload, method="aliexpress.ds.freight.query")

    # ----- frete oficial buyer (Fase 4b, read-only) --------------------------
    def calculate_buyer_freight(
        self,
        *,
        product_id: str,
        country_code: str = "BR",
        product_num: int = 1,
        send_goods_country_code: str = "CN",
        province_code: Optional[str] = None,
        city_code: Optional[str] = None,
        sku_id: Optional[str] = None,
        price: Optional[Decimal] = None,
        price_currency: Optional[str] = None,
    ) -> "AliExpressFreightResult":
        """Busca read-only aliexpress.logistics.buyer.freight.calculate.

        Não persiste nada. Usa o mesmo gateway /sync, sign_method=sha256 e o
        access_token como `session` (ainda não migramos para eco.taobao.com).
        O parâmetro de negócio `param_aeop_freight_calculate_for_buyer_d_t_o`
        é um JSON compacto e participa da assinatura. price/price_currency
        são opcionais e omitidos no primeiro smoke.
        """
        from .buyer_freight import (
            build_buyer_freight_params,
            normalize_buyer_freight_response,
        )

        business = build_buyer_freight_params(
            product_id=product_id,
            country_code=country_code,
            product_num=product_num,
            send_goods_country_code=send_goods_country_code,
            province_code=province_code,
            city_code=city_code,
            sku_id=sku_id,
            price=price,
            price_currency=price_currency,
        )
        payload = self.call(
            "aliexpress.logistics.buyer.freight.calculate", **business
        )
        return normalize_buyer_freight_response(
            payload, method="aliexpress.logistics.buyer.freight.calculate"
        )

    def get_buyer_freight(
        self,
        *,
        product_id: str,
        sku_id: str,
        country_code: str = "BR",
        product_num: int = 1,
        send_goods_country_code: str = "CN",
        province_code: Optional[str] = None,
        city_code: Optional[str] = None,
    ) -> "AliExpressFreightResult":
        """Busca read-only aliexpress.logistics.buyer.freight.get (isolado).

        Diferente do freight.calculate: o parâmetro externo é
        `aeopFreightCalculateForBuyerDTO` (JSON compacto que participa da
        assinatura). Exige o SKU canônico AliExpress (obtido via
        `canonical_freight_sku_id`). Mesmo gateway /sync,
        sign_method=sha256 e session=token. Não persiste nada.
        """
        from .buyer_freight_get import (
            build_buyer_freight_get_params,
            normalize_buyer_freight_get_response,
        )

        business = build_buyer_freight_get_params(
            product_id=product_id,
            country_code=country_code,
            product_num=product_num,
            send_goods_country_code=send_goods_country_code,
            province_code=province_code,
            city_code=city_code,
            sku_id=sku_id,
        )
        payload = self.call(
            "aliexpress.logistics.buyer.freight.get", **business
        )
        return normalize_buyer_freight_get_response(
            payload, method="aliexpress.logistics.buyer.freight.get"
        )


__all__: Iterable[str] = ["AliExpressClient", "DEFAULT_BASE_URL"]
