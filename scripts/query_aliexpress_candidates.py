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

#: Classificações exatas da comparação on/off (vocabulário fixo).
VERDICT_NO_DIFF = "nenhuma diferença observada; efeito inconclusivo"
VERDICT_DIFF = "diferença observada, ainda não atribuída ao parâmetro"
VERDICT_UNVIABLE = "comparação inviável (erro em ao menos uma chamada); efeito inconclusivo"

PARAMETER_NOTE = (
    "remove_personal_benefit sem respaldo na documentação oficial "
    "consultada (apiId 60452); tratado como experimental. Comportamento "
    "atual (true por padrão) preservado; não usado como garantia de "
    "exclusão de primeira compra."
)


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

    records = []
    for label, product_id, flag, rnd in plans:
        base = {
            "label": label,
            "product_id": product_id,
            "remove_personal_benefit": flag,
            "round": rnd,
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


def compare_benefit_records(records):
    """Classifica a comparação on/off por produto (vocabulário fixo).

    Só considera registros com flag e rodada (modo comparação). Iguais →
    efeito inconclusivo; diferentes → diferença ainda não atribuída ao
    parâmetro; erro em qualquer chamada da rodada → comparação inviável.
    """
    by_product = {}
    for record in records:
        if "remove_personal_benefit" not in record or "round" not in record:
            continue
        by_product.setdefault(
            (record.get("label"), record.get("product_id")), []
        ).append(record)

    comparisons = []
    for (label, product_id), items in by_product.items():
        flags = {r["remove_personal_benefit"] for r in items}
        if flags != {True, False}:
            continue
        rounds = {}
        for record in items:
            rounds.setdefault(record["round"], {})[
                record["remove_personal_benefit"]
            ] = record
        verdicts = []
        for idx in sorted(rounds):
            pair = rounds[idx]
            on, off = pair.get(True), pair.get(False)
            if (
                on is None
                or off is None
                or not on.get("ok")
                or not off.get("ok")
            ):
                verdicts.append({"round": idx, "verdict": VERDICT_UNVIABLE})
            elif on.get("detail") == off.get("detail"):
                verdicts.append({"round": idx, "verdict": VERDICT_NO_DIFF})
            else:
                verdicts.append({"round": idx, "verdict": VERDICT_DIFF})
        if all(v["verdict"] == VERDICT_NO_DIFF for v in verdicts):
            overall = VERDICT_NO_DIFF
        elif any(v["verdict"] == VERDICT_DIFF for v in verdicts):
            overall = VERDICT_DIFF
        else:
            overall = VERDICT_UNVIABLE
        comparisons.append(
            {
                "kind": "benefit_comparison",
                "label": label,
                "product_id": product_id,
                "experimental_parameter": True,
                "parameter_note": PARAMETER_NOTE,
                "rounds": verdicts,
                "overall": overall,
            }
        )
    return comparisons


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
