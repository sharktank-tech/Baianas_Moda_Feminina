"""Consulta read-only dos candidatos BR + controle internacional (investigação despacho BR).

Uso (shell com a sessão/credenciais do admin):
    ALIEXPRESS_APP_KEY=... ALIEXPRESS_APP_SECRET=... ALIEXPRESS_ACCESS_TOKEN=... \\
    python scripts/query_aliexpress_candidates.py

Somente `aliexpress.ds.product.get` com os defaults do painel (destino BR,
BRL, pt). Não importa, não publica, não compra, não persiste nada e não
imprime segredos: a saída é o resumo sanitizado
(`summarize_product_detail`) ou o erro sanitizado (`format_candidate_error`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from loja.integrations.aliexpress import (
    AliExpressConfigError,
    create_client_from_env,
    format_candidate_error,
    summarize_product_detail,
)

CANDIDATES = (
    ("BR-1", "1005013003211750"),
    ("BR-2", "1005012926123897"),
    ("BR-3", "1005010478234256"),
    ("INTL-CTRL", "1005009519310498"),
)


def main() -> int:
    try:
        client = create_client_from_env(require_token=True)
    except AliExpressConfigError as exc:
        print(
            json.dumps(
                {"ok": False, "error": format_candidate_error(exc)},
                ensure_ascii=False,
            )
        )
        return 2

    failed = 0
    for label, product_id in CANDIDATES:
        try:
            detail = client.get_product(product_id=product_id)
        except Exception as exc:  # noqa: BLE001 (registro sanitizado)
            print(
                json.dumps(
                    {
                        "ok": False,
                        "label": label,
                        "product_id": product_id,
                        "error": format_candidate_error(exc),
                    },
                    ensure_ascii=False,
                )
            )
            failed += 1
            continue
        if detail is None:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "label": label,
                        "product_id": product_id,
                        "error": {
                            "type": "NotFound",
                            "code": None,
                            "sub_code": None,
                            "message": "produto não retornado",
                        },
                    },
                    ensure_ascii=False,
                )
            )
            failed += 1
            continue
        print(
            json.dumps(
                {
                    "ok": True,
                    "label": label,
                    "product_id": product_id,
                    "detail": summarize_product_detail(detail),
                },
                ensure_ascii=False,
            )
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
