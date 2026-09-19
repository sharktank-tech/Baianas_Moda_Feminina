#!/usr/bin/env python3
"""
Verificação somente leitura para futuro canário orgânico (Fase 8.0.11I).

Uso:
  DATABASE_URL=postgresql://... python scripts/verify_future_canary_readonly.py
  CANARY_ORDER_ID=<uuid> DATABASE_URL=... python scripts/verify_future_canary_readonly.py

Garantias:
  - SOMENTE LEITURA: transação explícita READ ONLY, verifica transaction_read_only=on, encerra com ROLLBACK.
  - Nunca expõe PII: CPF, endereço completo, tokens, secrets são redigidos.
  - Exclui o pedido legado que já atingiu 5 tentativas (attempt_count == 5).
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from decimal import Decimal, InvalidOperation

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def _sanitize(val: str | None) -> str:
    if not val:
        return "ausente"
    return "presente"


def _mask_uuid(val: uuid.UUID | str | None) -> str:
    if not val:
        return "ausente"
    s = str(val)
    return f"{s[:8]}...{s[-4:]}"


def _is_valid_br_mobile(phone: str | None) -> bool:
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 13 and digits.startswith("55"):
        digits = digits[2:]
    return bool(re.fullmatch(r"[1-9][0-9]9[0-9]{8}", digits))


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("DATABASE_URL não configurada", file=sys.stderr)
        return 2

    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        print(f"DATABASE_URL não é postgresql: {url.get_backend_name()}", file=sys.stderr)
        return 2
    if url.host not in ("127.0.0.1", "localhost", "::1") and os.environ.get("CANARY_READONLY") != "1":
        print(f"Host não descartável e CANARY_READONLY !=1: {url.host} — abortando", file=sys.stderr)
        return 2

    engine = create_engine(database_url)

    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            ro = conn.execute(text("SELECT current_setting('transaction_read_only')")).scalar()
            if str(ro).lower() != "on":
                print(f"FAIL: transaction_read_only != on (obtido {ro!r})", file=sys.stderr)
                trans.rollback()
                return 2
            show_ro = conn.execute(text("SHOW transaction_read_only")).scalar()
            if str(show_ro).lower() != "on":
                print(f"FAIL: SHOW transaction_read_only != on (obtido {show_ro!r})", file=sys.stderr)
                trans.rollback()
                return 2
            canary_schema = os.environ.get("CANARY_SCHEMA")
            if canary_schema:
                if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", canary_schema):
                    print(f"FAIL: CANARY_SCHEMA inválido", file=sys.stderr)
                    trans.rollback()
                    return 2
                conn.execute(text(f"SET search_path TO {canary_schema}"))
        except Exception:
            try:
                trans.rollback()
            except Exception:
                pass
            print(f"FAIL: não foi possível estabelecer transação READ ONLY", file=sys.stderr)
            return 2

        try:
            target_order_id = os.environ.get("CANARY_ORDER_ID")
            candidates = []
            if target_order_id:
                try:
                    uuid.UUID(str(target_order_id))
                except Exception:
                    print(f"FAIL: CANARY_ORDER_ID inválido", file=sys.stderr)
                    trans.rollback()
                    return 2
                row = conn.execute(
                    text(
                        """
                        SELECT ao.id, ao.order_id, ao.supplier_id, ao.state, ao.attempt_count, ao.ali_order_id
                        FROM aliexpress_orders ao
                        WHERE ao.id = :id
                        """
                    ),
                    {"id": str(target_order_id)},
                ).fetchone()
                if row is None:
                    print(f"Nenhum pedido encontrado para CANARY_ORDER_ID {_mask_uuid(target_order_id)}")
                    trans.rollback()
                    return 1
                ao_id, order_id, supplier_id, state, attempt_count, ali_order_id = row
                if order_id:
                    order_info = conn.execute(
                        text("SELECT status, total, acquisition_source FROM orders WHERE id = :oid"),
                        {"oid": str(order_id)},
                    ).fetchone()
                    if order_info:
                        order_status, total, acquisition_source = order_info
                    else:
                        order_status, total, acquisition_source = None, None, None
                else:
                    order_status, total, acquisition_source = None, None, None
                if state != "ready" or attempt_count != 0 or (ali_order_id and str(ali_order_id).strip()):
                    print(f"FAIL: candidato por ID não elegível state={state} attempt_count={attempt_count} ali_order_id={_sanitize(ali_order_id)}")
                    trans.rollback()
                    return 1
                if attempt_count == 5:
                    print("FAIL: candidato é legado com 5 tentativas (excluído)")
                    trans.rollback()
                    return 1
                attempts = conn.execute(
                    text("SELECT count(*) FROM aliexpress_submission_attempts WHERE aliexpress_order_id = :id"),
                    {"id": str(ao_id)},
                ).scalar()
                remotes = conn.execute(
                    text("SELECT count(*) FROM aliexpress_remote_orders WHERE aliexpress_order_id = :id"),
                    {"id": str(ao_id)},
                ).scalar()
                if attempts != 0 or remotes != 0:
                    print(f"FAIL: candidato por ID tem tentativas/remota attempts={attempts} remotes={remotes}")
                    trans.rollback()
                    return 1
                candidates = [(ao_id, order_id, supplier_id, state, attempt_count, ali_order_id, order_status, total, acquisition_source)]
            else:
                rows = conn.execute(
                    text(
                        """
                        SELECT ao.id, ao.order_id, ao.supplier_id, ao.state, ao.attempt_count, ao.ali_order_id,
                               o.status as order_status, o.total, o.acquisition_source
                        FROM aliexpress_orders ao
                        JOIN orders o ON o.id = ao.order_id
                        WHERE ao.state = 'ready'
                          AND ao.attempt_count = 0
                          AND (ao.ali_order_id IS NULL OR ao.ali_order_id = '')
                        ORDER BY ao.queued_at DESC NULLS LAST, ao.created_at DESC
                        LIMIT 5
                        """
                    )
                ).fetchall()
                for r in rows:
                    ao_id, order_id, supplier_id, state, attempt_count, ali_order_id, order_status, total, acquisition_source = r
                    if attempt_count == 5:
                        print(f"Excluído candidato {_mask_uuid(ao_id)}: legado 5 tentativas")
                        continue
                    attempts = conn.execute(
                        text("SELECT count(*) FROM aliexpress_submission_attempts WHERE aliexpress_order_id = :id"),
                        {"id": str(ao_id)},
                    ).scalar()
                    remotes = conn.execute(
                        text("SELECT count(*) FROM aliexpress_remote_orders WHERE aliexpress_order_id = :id"),
                        {"id": str(ao_id)},
                    ).scalar()
                    if attempts != 0 or remotes != 0:
                        print(f"Excluído candidato {_mask_uuid(ao_id)}: attempts={attempts} remotes={remotes}")
                        continue
                    candidates.append(r)
                    break

            if not candidates:
                print("Nenhum pedido orgânico novo elegível encontrado (ready, sem tentativas/remota, não legado 5 tentativas)")
                trans.rollback()
                return 1

            ao_id, order_id, supplier_id, state, attempt_count, ali_order_id, order_status, total, acquisition_source = candidates[0]
            print(f"candidato encontrado: ali_order={_mask_uuid(ao_id)} order={_mask_uuid(order_id)} state={state} attempt_count={attempt_count} order_status={order_status}")

            # Origem de aquisição: orders.acquisition_source comprova atribuição de tráfego,
            # mas cpc, direct ou domínio externo não comprovam pedido orgânico de cliente real.
            # Nunca imprime o valor bruto.
            if acquisition_source is None or not str(acquisition_source).strip():
                print("origem orgânica não comprovada")
                trans.rollback()
                return 1
            normalized_acq = str(acquisition_source).strip().lower()
            non_organic = {"", "test", "unknown", "canary", "pg.test", "pg_test", "test canary", "teste", "11999999999"}
            if normalized_acq in non_organic:
                print("origem orgânica não comprovada")
                trans.rollback()
                return 1
            if re.fullmatch(r"\d{11}", normalized_acq):
                print("origem orgânica não comprovada")
                trans.rollback()
                return 1
            if re.search(r"[@:/]", normalized_acq) and not re.fullmatch(r"[a-z0-9._-]+\.[a-z]{2,}", normalized_acq):
                if "@" in normalized_acq or ":" in normalized_acq:
                    print("origem orgânica não comprovada")
                    trans.rollback()
                    return 1
            # Registra que há origem de aquisição, mas não comprova orgânico por si só
            print("origem de aquisição registrada")
            # Evidência orgânica independente será verificada após demais validações;
            # por enquanto, apenas registra aquisição e segue para pagamento, snapshot, etc.

            # 1) Pagamento canônico aprovado, valor e moeda - exige orders.status == 'paid' e provider_payment_id presente
            if order_status != "paid":
                print(f"FAIL: orders.status não é paid (obtido {order_status})")
                trans.rollback()
                return 1
            if total is None:
                print("FAIL: order total ausente (evidência faltando)")
                trans.rollback()
                return 1
            pay = conn.execute(
                text(
                    """
                    SELECT id, status, amount, currency, provider_payment_id, created_at
                    FROM payments
                    WHERE order_id = :oid
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """
                ),
                {"oid": str(order_id)},
            ).fetchone()
            if not pay:
                print(f"FAIL: pagamento ausente para order {_mask_uuid(order_id)}")
                trans.rollback()
                return 1
            pay_id, pay_status, pay_amount, pay_currency, pay_provider_id, pay_created = pay
            if pay_amount is None:
                print("FAIL: pagamento amount ausente")
                trans.rollback()
                return 1
            if pay_currency is None:
                print("FAIL: pagamento currency ausente")
                trans.rollback()
                return 1
            if not pay_provider_id or not str(pay_provider_id).strip():
                print("FAIL: provider_payment_id ausente")
                trans.rollback()
                return 1
            print(f"Pagamento latest: id={_mask_uuid(pay_id)} status={pay_status} currency={pay_currency}")
            if pay_status != "approved":
                print(f"FAIL: pagamento não aprovado (status={pay_status})")
                trans.rollback()
                return 1
            if str(pay_currency).upper() != "BRL":
                print(f"FAIL: moeda incompatível pay={pay_currency} != BRL")
                trans.rollback()
                return 1
            try:
                pay_dec = Decimal(str(pay_amount))
                total_dec = Decimal(str(total))
            except (InvalidOperation, ValueError, TypeError):
                print(f"FAIL: valor incomparável")
                trans.rollback()
                return 1
            if pay_dec.quantize(Decimal("0.01")) != total_dec.quantize(Decimal("0.01")):
                print(f"FAIL: valor divergente")
                trans.rollback()
                return 1
            print("OK: pagamento canônico aprovado, valor e moeda BRL conferem")

            # 2) Dados de entrega necessários (sem expor valores)
            order_row = conn.execute(
                text(
                    """
                    SELECT customer_name, customer_phone, zip_code, street, number, city, state,
                           customer_email
                    FROM orders WHERE id = :oid
                    """
                ),
                {"oid": str(order_id)},
            ).fetchone()
            if not order_row:
                print("FAIL: order não encontrado")
                trans.rollback()
                return 1
            cname, cphone, zipc, street, number, city, state_uf, cemail = order_row
            checks = {
                "customer_name": cname,
                "customer_phone": cphone,
                "zip_code": zipc,
                "street": street,
                "number": number,
                "city": city,
                "state": state_uf,
            }
            missing = [k for k, v in checks.items() if not (v and str(v).strip())]
            if missing:
                print(f"FAIL: dados de entrega ausentes: {missing}")
                trans.rollback()
                return 1
            if not _is_valid_br_mobile(cphone):
                print("FAIL: telefone não é móvel BR válido (11 dígitos)")
                trans.rollback()
                return 1
            print(f"OK: dados de entrega presentes (zip {_sanitize(zipc)}, city {_sanitize(city)}, phone {_sanitize(cphone)})")

            # 3) Snapshot íntegro (1:1) e correspondência
            snap = conn.execute(
                text("SELECT id, shipping_address, items, created_at FROM aliexpress_submission_snapshots WHERE aliexpress_order_id = :id"),
                {"id": str(ao_id)},
            ).fetchone()
            if not snap:
                print("FAIL: snapshot ausente (não materializado)")
                trans.rollback()
                return 1
            snap_id, shipping_address, items, snap_created = snap
            if not shipping_address:
                print("FAIL: snapshot shipping_address ausente")
                trans.rollback()
                return 1
            if not items or not isinstance(items, list) or len(items) == 0:
                print("FAIL: snapshot items ausente ou vazio")
                trans.rollback()
                return 1
            print(f"Snapshot: id={_mask_uuid(snap_id)} created={snap_created} items={len(items)}")
            required_keys = {"contact_person", "full_name", "mobile_no", "phone_country", "zip", "address", "city", "province", "country"}
            if not required_keys.issubset(set(shipping_address.keys())):
                print(f"FAIL: snapshot shipping_address incompleto")
                trans.rollback()
                return 1
            if shipping_address.get("country") != "BR" or shipping_address.get("phone_country") != "+55":
                print("FAIL: snapshot country/phone_country divergente")
                trans.rollback()
                return 1
            for idx, it in enumerate(items):
                if not all(k in it for k in ("order_item_id", "product_id", "sku_attr", "product_count")):
                    print(f"FAIL: snapshot item {idx} incompleto")
                    trans.rollback()
                    return 1
                if not it.get("sku_attr") or not str(it["sku_attr"]).strip():
                    print(f"FAIL: snapshot item {idx} sku_attr ausente")
                    trans.rollback()
                    return 1
            # Correspondência entre todos os order_items, aliexpress_order_items e snapshot
            order_items = conn.execute(text("SELECT id FROM order_items WHERE order_id = :oid"), {"oid": str(order_id)}).fetchall()
            aoi_rows = conn.execute(text("SELECT order_item_id FROM aliexpress_order_items WHERE aliexpress_order_id = :aid"), {"aid": str(ao_id)}).fetchall()
            snapshot_ids_list = [str(it.get("order_item_id")) for it in items]
            order_item_ids = {str(r[0]) for r in order_items}
            aoi_ids = {str(r[0]) for r in aoi_rows}
            snapshot_ids = set(snapshot_ids_list)
            # Exige mesma quantidade
            if not (len(order_items) == len(aoi_rows) == len(items)):
                print(f"FAIL: quantidade divergente order_items={len(order_items)} aliexpress_order_items={len(aoi_rows)} snapshot={len(items)}")
                trans.rollback()
                return 1
            # Rejeita order_item_id duplicado
            if len(order_item_ids) != len(order_items) or len(aoi_ids) != len(aoi_rows) or len(snapshot_ids) != len(snapshot_ids_list):
                print(f"FAIL: order_item_id duplicado")
                trans.rollback()
                return 1
            if order_item_ids != aoi_ids or order_item_ids != snapshot_ids or aoi_ids != snapshot_ids:
                print(f"FAIL: correspondência ausente entre order_items ({len(order_item_ids)}), aliexpress_order_items ({len(aoi_ids)}) e snapshot ({len(snapshot_ids)})")
                trans.rollback()
                return 1
            if len(order_items) == 0:
                print("FAIL: nenhum order_item encontrado")
                trans.rollback()
                return 1
            print("OK: snapshot íntegro (1:1, shipping + items) e correspondência completa")

            # 4) Economia válida (todos os itens)
            econ_rows = conn.execute(
                text(
                    """
                    SELECT p.pricing_status, p.target_margin_percent, p.price,
                           sp.cost, sp.shipping_cost, sp.landed_cost, sp.currency, sp.freight_currency, sp.freight_status,
                           aoi.product_id, pv.supplier_product_id
                    FROM aliexpress_order_items aoi
                    JOIN products p ON p.id = aoi.product_id
                    JOIN product_variants pv ON pv.id = aoi.variant_id
                    JOIN supplier_products sp ON sp.id = pv.supplier_product_id
                    WHERE aoi.aliexpress_order_id = :id
                    """
                ),
                {"id": str(ao_id)},
            ).fetchall()
            if not econ_rows:
                print("FAIL: nenhum item de economia encontrado (aliexpress_order_items vazio)")
                trans.rollback()
                return 1
            for idx, r in enumerate(econ_rows):
                pricing_status, target_margin, price, cost, shipping, landed, curr, freight_curr, freight_status, prod_id, sp_id = r
                if pricing_status != "priced":
                    print(f"FAIL: item {idx} pricing_status não é priced ({pricing_status})")
                    trans.rollback()
                    return 1
                if target_margin is None:
                    print(f"FAIL: item {idx} target_margin_percent ausente")
                    trans.rollback()
                    return 1
                try:
                    margin = Decimal(str(target_margin))
                except (InvalidOperation, ValueError, TypeError):
                    print(f"FAIL: item {idx} target_margin_percent inválido")
                    trans.rollback()
                    return 1
                if not (Decimal("0") < margin < Decimal("100")):
                    print(f"FAIL: item {idx} target_margin_percent fora de (0,100)")
                    trans.rollback()
                    return 1
                if cost is None:
                    print(f"FAIL: item {idx} cost ausente")
                    trans.rollback()
                    return 1
                if shipping is None:
                    print(f"FAIL: item {idx} shipping_cost ausente")
                    trans.rollback()
                    return 1
                if landed is None:
                    print(f"FAIL: item {idx} landed_cost ausente")
                    trans.rollback()
                    return 1
                if not curr or not freight_curr:
                    print(f"FAIL: item {idx} moeda ausente")
                    trans.rollback()
                    return 1
                if str(curr).upper() != "BRL" or str(freight_curr).upper() != "BRL":
                    print(f"FAIL: item {idx} moeda incompatível")
                    trans.rollback()
                    return 1
                if str(curr).upper() != str(freight_curr).upper():
                    print(f"FAIL: item {idx} moeda incompatível curr vs freight_curr")
                    trans.rollback()
                    return 1
                if freight_status != "available":
                    print(f"FAIL: item {idx} freight_status não é available")
                    trans.rollback()
                    return 1
                try:
                    cost_dec = Decimal(str(cost))
                    ship_dec = Decimal(str(shipping))
                    landed_dec = Decimal(str(landed))
                    expected = (cost_dec + ship_dec).quantize(Decimal("0.01"))
                    if landed_dec.quantize(Decimal("0.01")) != expected:
                        print(f"FAIL: item {idx} landed_cost divergente")
                        trans.rollback()
                        return 1
                except (InvalidOperation, ValueError, TypeError):
                    print(f"FAIL: item {idx} cálculo inválido")
                    trans.rollback()
                    return 1
            print(f"OK: economia válida para {len(econ_rows)} item(ns) (priced, margem, frete, landed)")

            # 5) Ausência de tentativas ou identidade remota anteriores
            attempts = conn.execute(
                text("SELECT count(*) FROM aliexpress_submission_attempts WHERE aliexpress_order_id = :id"), {"id": str(ao_id)}
            ).scalar()
            remotes = conn.execute(
                text("SELECT count(*) FROM aliexpress_remote_orders WHERE aliexpress_order_id = :id"), {"id": str(ao_id)}
            ).scalar()
            ali_order_id_present = conn.execute(text("SELECT ali_order_id FROM aliexpress_orders WHERE id = :id"), {"id": str(ao_id)}).scalar()
            if attempts != 0 or remotes != 0 or (ali_order_id_present and str(ali_order_id_present).strip()):
                print(f"FAIL: tentativas/remota já existentes attempts={attempts} remotes={remotes} ali_order_id={_sanitize(ali_order_id_present)}")
                trans.rollback()
                return 1
            print("OK: ausência de tentativas e identidade remota (novo orgânico)")

            print(f"OK: pedido legado com 5 tentativas excluído (attempt_count !=5)")

            # Todos os checks preliminares aprovados; confirmação orgânica pendente até evidência independente
            print("checks preliminares aprovados; confirmação orgânica pendente")
            still_ro = conn.execute(text("SELECT current_setting('transaction_read_only')")).scalar()
            if str(still_ro).lower() != "on":
                print(f"FAIL: transação deixou de ser read-only antes do rollback", file=sys.stderr)
                trans.rollback()
                return 2
            trans.rollback()
            return 1
        except Exception:
            try:
                trans.rollback()
            except Exception:
                pass
            print(f"FAIL: erro inesperado na verificação", file=sys.stderr)
            return 1
        finally:
            try:
                if trans.is_active:
                    trans.rollback()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
