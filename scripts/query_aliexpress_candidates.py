"""Consulta read-only dos candidatos BR + controle internacional (investigação despacho BR).

Uso (shell com a sessão/credenciais do admin):
    ALIEXPRESS_APP_KEY=... ALIEXPRESS_APP_SECRET=... ALIEXPRESS_ACCESS_TOKEN=... \\
    python scripts/query_aliexpress_candidates.py [--compare-benefit]

Somente `aliexpress.ds.product.get` com mercado fixo (destino BR, BRL,
pt). `--compare-benefit` consulta os produtos de comparação com a flag
ligada e desligada, repetindo em sequência alternada (`--repeat N`); cada
registro carrega flag, rodada e mercado. A classificação usa exatamente:
iguais → "nenhuma diferença observada; efeito inconclusivo"; diferentes →
"diferença observada, ainda não atribuída ao parâmetro". O parâmetro é
experimental (sem respaldo na doc oficial consultada) e não garante
exclusão de primeira compra. Não importa, não publica, não compra,
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
    BENEFIT_MARKET,
    compare_benefit_records,
    create_client_from_env,
    fetch_product_record,
    format_candidate_error,
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

#: Mercado fixo da investigação (igual ao do painel; ver BENEFIT_MARKET).
FIXED_MARKET = dict(BENEFIT_MARKET)


def query_candidates(client, *, compare_benefit=False, repeat=1):
    """Executa as consultas e devolve (registros, falhas).

    `client` é injetável (testes usam fake, sem rede). Cada registro carrega
    `remove_personal_benefit`, `round` e o mercado fixo, para auditoria. Na
    comparação, as rodadas alternam a ordem das flags (True,False /
    False,True, ...) para reduzir interferência de mudanças de preço.
    """
    plans = []
    if compare_benefit:
        compared_ids = {pid for _, pid in COMPARE_PRODUCTS}
        for rnd in range(max(int(repeat), 1)):
            first, second = (True, False) if rnd % 2 == 0 else (False, True)
            for label, product_id in COMPARE_PRODUCTS:
                plans.append((label, product_id, first, rnd))
                plans.append((label, product_id, second, rnd))
        for label, product_id in CANDIDATES:
            if product_id not in compared_ids:
                plans.append((label, product_id, True, 0))
    else:
        for label, product_id in CANDIDATES:
            plans.append((label, product_id, True, 0))

    records = [
        fetch_product_record(
            client,
            product_id=product_id,
            label=label,
            remove_personal_benefit=flag,
            round=rnd,
            market=FIXED_MARKET,
        )
        for label, product_id, flag, rnd in plans
    ]
    failed = sum(1 for record in records if not record["ok"])
    return records, failed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--compare-benefit",
        action="store_true",
        help="consulta os produtos de comparação com remove_personal_benefit on/off",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="repetições da comparação, alternando a ordem das flags (padrão: 1)",
    )
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat deve ser >= 1")

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
        client, compare_benefit=args.compare_benefit, repeat=args.repeat
    )
    for record in records:
        print(json.dumps(record, ensure_ascii=False))
    if args.compare_benefit:
        for comparison in compare_benefit_records(records):
            print(json.dumps(comparison, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
