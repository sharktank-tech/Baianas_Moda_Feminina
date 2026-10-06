"""Consulta read-only dos candidatos BR + controle internacional (investigação despacho BR).

Uso (shell com a sessão/credenciais do admin):
    ALIEXPRESS_APP_KEY=... ALIEXPRESS_APP_SECRET=... ALIEXPRESS_ACCESS_TOKEN=... \\
    python scripts/query_aliexpress_candidates.py [--compare-benefit]

Somente `aliexpress.ds.product.get` com mercado fixo (destino BR, BRL,
pt). `--compare-benefit` consulta os produtos de comparação duas vezes
(`remove_personal_benefit` ligado e desligado); cada registro carrega a
flag usada — saídas idênticas com flags diferentes são evidência de
parâmetro ignorado, nunca sucesso. Não importa, não publica, não compra,
não persiste nada e não imprime segredos: a saída é o resumo sanitizado
(`summarize_product_detail`) ou o erro sanitizado (`format_candidate_error`).
"""

from __future__ import annotations

import argparse
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
    ("BR-4", "1005010502074833"),
    ("INTL-CTRL", "1005009519310498"),
)

#: Produtos da comparação on/off de `remove_personal_benefit`.
COMPARE_PRODUCTS = (
    ("BR-3", "1005010478234256"),
    ("BR-4", "1005010502074833"),
)

#: Mercado fixo da investigação (igual ao do painel).
FIXED_MARKET = {
    "ship_to_country": "BR",
    "target_currency": "BRL",
    "target_language": "pt",
}

_NOT_FOUND = {
    "type": "NotFound",
    "code": None,
    "sub_code": None,
    "message": "produto não retornado",
}


def query_candidates(client, *, compare_benefit=False):
    """Executa as consultas e devolve (registros, falhas).

    `client` é injetável (testes usam fake, sem rede). Cada registro carrega
    `remove_personal_benefit` e o mercado fixo, para auditoria.
    """
    plans = []
    if compare_benefit:
        compared_ids = {pid for _, pid in COMPARE_PRODUCTS}
        for label, product_id in COMPARE_PRODUCTS:
            plans.append((label, product_id, True))
            plans.append((label, product_id, False))
        for label, product_id in CANDIDATES:
            if product_id not in compared_ids:
                plans.append((label, product_id, True))
    else:
        for label, product_id in CANDIDATES:
            plans.append((label, product_id, True))

    records = []
    for label, product_id, flag in plans:
        base = {
            "label": label,
            "product_id": product_id,
            "remove_personal_benefit": flag,
            "market": dict(FIXED_MARKET),
        }
        try:
            detail = client.get_product(
                product_id=product_id, remove_personal_benefit=flag
            )
        except Exception as exc:  # noqa: BLE001 (registro sanitizado)
            records.append({**base, "ok": False, "error": format_candidate_error(exc)})
            continue
        if detail is None:
            records.append({**base, "ok": False, "error": dict(_NOT_FOUND)})
            continue
        records.append({**base, "ok": True, "detail": summarize_product_detail(detail)})
    failed = sum(1 for record in records if not record["ok"])
    return records, failed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--compare-benefit",
        action="store_true",
        help="consulta os produtos de comparação com remove_personal_benefit on/off",
    )
    args = parser.parse_args(argv)

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

    records, failed = query_candidates(
        client, compare_benefit=args.compare_benefit
    )
    for record in records:
        print(json.dumps(record, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
