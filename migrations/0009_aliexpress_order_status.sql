-- Migration 0009: AliExpress order status timeline (Fase 8.0.6)
-- — Idempotente, restrita ao schema `baianas_moda_feminina`. Nunca toca `public`.
--
-- Contexto:
--   * 0007 -> pipeline de fulfillment AliExpress (AliExpressOrder, estados,
--             credencial criptografada, idempotency_key, submitted_at,
--             confirmed_at, cancelled_at).
--   * 0008 -> reconciliation/pending_reconciliation + attempt_key.
--   * 0009 (esta) -> ciclo de vida logístico pós-submissão
--             (submitted -> confirmed -> shipped -> delivered) por meio do
--             boundary de status (HARD-OFF). Adiciona os marcos temporais de
--             envio e entrega.
--
-- Justificativa dos campos novos (requisito 8.0.6):
--   1. `aliexpress_orders.shipped_at` (TIMESTAMP, nullable)
--        Marco de envio: preenchido quando o status sync observa confirmed/
--        shipped/delivered (primeira passagem somente; nunca sobrescrito).
--   2. `aliexpress_orders.delivered_at` (TIMESTAMP, nullable)
--        Marco de entrega: preenchido quando o status sync observa delivered
--        (primeira passagem somente; nunca sobrescrito).
--   A CHECK `ck_aliexpress_order_state` já contém confirmed/shipped/delivered/
--   cancelled/cancel_requested — NENHUMA constraint precisa ser recriada.
--
-- Aplicação (uma ou mais vezes, PostgreSQL):
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0009_aliexpress_order_status.sql

\set ON_ERROR_STOP on

-- Restringe toda a migration ao schema dedicado da baianas_moda_feminina.
SET search_path TO baianas_moda_feminina;

-- ============================================================
-- 1) Marcos temporais do ciclo de vida logístico
-- ============================================================
ALTER TABLE aliexpress_orders
    ADD COLUMN IF NOT EXISTS shipped_at TIMESTAMP;

ALTER TABLE aliexpress_orders
    ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMP;

-- ============================================================
-- Verificação de schema public invariável
-- ============================================================
-- Esta migration NÃO cria objetos no schema public. O projeto real possui uma
-- public.aliexpress_orders legada/preexistente, fora do domínio da baianas_moda_feminina;
-- sua mera existência não pode ser tratada como evidência de mutação por esta
-- migration. Os objetos NOVOS introduzidos pelo fulfillment (credentials/items)
-- continuam proibidos em public.
DO $$
DECLARE
    obj_count integer;
BEGIN
    SELECT count(*) INTO obj_count
    FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name IN ('aliexpress_credentials', 'aliexpress_order_items');
    IF obj_count > 0 THEN
        RAISE EXCEPTION 'Migration 0009 criou objetos novos no schema public (proibido)';
    END IF;
END $$;