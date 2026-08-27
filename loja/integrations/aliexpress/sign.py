"""Signer determinístico do protocolo TOP (router/rest) da AliExpress Open Platform.

Algoritmo oficial (articleId 105423 / 108974):
  1. Coletar todos os parâmetros da requisição, EXCETO `sign`.
  2. Ordenar os nomes dos parâmetros em ordem ASCII crescente.
  3. Concatenar cada par `nome + valor` (sem separador).
  4. Envolver com o app_secret: `secret + concatenado + secret`.
  5. MD5 (ou HMAC_MD5 com a chave = app_secret) -> hex uppercase.

Vetor golden (oficial, doc 105423):
  secret = "hotel"
  params = method=taobao.xhotel.update, app_key=12345678, session=test,
           timestamp=2016-01-01 12:00:00, format=json, v=2.0,
           sign_method=md5, name=GJ001, outer_id=GJ001
  -> sign esperado: 66987CB115214E59E6EC978214934FB8
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Iterable, Mapping

from .errors import AliExpressConfigError, redact

SignMethod = str  # "md5" | "hmac"


def sign_top(params: Mapping[str, str], secret: str, *, method: SignMethod = "md5") -> str:
    """Calcula o `sign` TOP para `params`.

    O próprio parâmetro `sign` é ignorado do material assinado (contrato TOP:
    "todos os parâmetros exceto sign").
    """
    if not secret:
        raise AliExpressConfigError("app_secret ausente para assinatura")

    ordered = "".join(
        f"{k}{params[k]}" for k in sorted(params.keys()) if k != "sign"
    )

    method = method.lower()
    if method == "md5":
        raw = secret + ordered + secret
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()
    elif method in ("hmac", "hmac-md5"):
        digest = hmac.new(
            secret.encode("utf-8"), ordered.encode("utf-8"), hashlib.md5
        ).hexdigest()
    else:
        raise AliExpressConfigError(f"sign_method não suportado: {method!r} (use md5/hmac)")

    return digest.upper()


def redact_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Retorna cópia segura dos parâmetros para logs (mascara secret/token/sign)."""
    return redact(dict(params))


__all__: Iterable[str] = ["sign_top", "redact_params", "SignMethod"]
