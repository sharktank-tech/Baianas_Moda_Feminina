"""Smoke REAL read-only da AliExpress (aliexpress.ds.text.search).

USA APENAS variáveis de ambiente (nunca imprime seus valores):
  ALIEXPRESS_APP_KEY
  ALIEXPRESS_APP_SECRET
  ALIEXPRESS_ACCESS_TOKEN
  ALIEXPRESS_BASE_URL   (opcional)

Comportamento:
  - Chamada real, read-only, sem gravar nada no banco.
  - Imprime SOMENTE informações sanitizadas (gateway, método, status, etc.).
  - NÃO imprime session/access_token/app_secret/sign/body/DATABASE_URL.
  - Se hmac falhar com erro de assinatura, PARAR e reportar (não troca p/ md5).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import requests  # noqa: E402

from loja.integrations.aliexpress import (  # noqa: E402
    AliExpressAPIError,
    AliExpressAuthError,
    AliExpressConfigError,
    AliExpressRateLimitError,
    AliExpressTransportError,
    create_client_from_env,
)
from loja.integrations.aliexpress.transport import (  # noqa: E402
    RequestsTransport,
    TransportError,
    TransportTimeout,
)

METHOD = "aliexpress.ds.text.search"
SIGN_ERROR_CODES = {"50", "27", "40", "41"}
SIGN_ERROR_SUBSTRINGS = ("sign", "signature", "illegal sign")


class _CapturingTransport(RequestsTransport):
    last_status: int | None = None

    def post(self, params: dict) -> str:
        try:
            resp = requests.post(
                self.base_url,
                data=params,
                timeout=self.timeout,
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
            )
        except requests.Timeout as exc:
            raise TransportTimeout(f"timeout após {self.timeout}s") from exc
        except requests.RequestException as exc:
            raise TransportError(f"falha de rede: {type(exc).__name__}") from exc
        self.last_status = resp.status_code
        if resp.status_code != 200:
            raise TransportError(f"HTTP {resp.status_code}")
        return resp.text


def _find_request_id(obj: object) -> str | None:
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


def _sanitize(msg: str) -> str:
    low = (msg or "").lower()
    if any(s in low for s in SIGN_ERROR_SUBSTRINGS):
        return f"{msg} [ERRO DE ASSINATURA — não trocar automaticamente para md5; parar e reportar]"
    return msg


def main() -> int:
    try:
        client = create_client_from_env(require_token=True, transport=_CapturingTransport(
            os.environ.get("ALIEXPRESS_BASE_URL") or "https://api.aliexpress.com/router/rest",
            timeout=10.0,
        ))
    except AliExpressConfigError as exc:
        print(f"[smoke] config incompleta: credenciais nao configuradas no ambiente", file=sys.stderr)
        print(f"[smoke] (detalhe interno: {exc.message})", file=sys.stderr)
        return 2

    print(f"gateway: {client.base_url}")
    print(f"method: {METHOD}")
    print(f"sign_method: {client.sign_method}")
    print("auth/session: configured")

    try:
        payload = client.call(
            METHOD,
            keyWord="t shirt",
            local="en_US",
            countryCode="BR",
            currency="BRL",
            pageIndex=1,
            pageSize=5,
            sortBy="orders,desc",
        )
    except AliExpressTransportError as exc:
        print(f"HTTP status: <transporte> ({type(exc).__name__})")
        print(f"gateway usado: {client.base_url}")
        print(f"erro sanitizado: {_sanitize(str(exc))}")
        return 1
    except (AliExpressAuthError, AliExpressRateLimitError, AliExpressAPIError) as exc:
        code = getattr(exc, "code", None)
        print(f"HTTP status: {getattr(client._transport, 'last_status', None)}")
        print(f"error code: {code}")
        print(f"error message: {_sanitize(exc.message)}")
        rid = _find_request_id(getattr(exc, "raw", None))
        if rid:
            print(f"request_id: {rid}")
        print(f"gateway usado: {client.base_url}")
        return 1

    status = getattr(client._transport, "last_status", None)
    wrapper = f"{METHOD}_response"
    products = _extract_products(payload)
    print(f"HTTP status: {status}")
    print(f"wrapper JSON: {wrapper}")
    print("API result: success")
    print(f"quantidade de produtos: {len(products)}")
    if products:
        first = products[0]
        print("campos do primeiro item: " + ", ".join(sorted(first.keys())))
    print("request sanitizada: method=%s local=en_US countryCode=BR currency=BRL pageIndex=1 pageSize=5 sortBy=orders,desc keyWord=<redacted>" % METHOD)
    return 0


def _extract_products(payload: object) -> list:
    """Extrai tolerante a lista de produtos de formatos comuns de resposta."""
    if isinstance(payload, dict):
        for cand in ("products", "itemList", "items", "productList"):
            v = payload.get(cand)
            if isinstance(v, list):
                return v
        result = payload.get("result")
        if isinstance(result, dict):
            for cand in ("products", "itemList", "items"):
                v = result.get(cand)
                if isinstance(v, list):
                    return v
    return []


if __name__ == "__main__":
    raise SystemExit(main())
