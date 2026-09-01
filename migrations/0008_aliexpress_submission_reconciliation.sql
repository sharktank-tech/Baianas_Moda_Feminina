-- Migration 0008: AliExpress submission reconciliation (Fase 8.0.5)
-- — Idempotente, restrita ao schema `pollomodas`. Nunca toca `public`.
--
-- Contexto:
--   * 0007 -> pipeline de fulfillment AliExpress (credencial criptografada,
--             AliExpressOrder/AliExpressOrderItem, estados, idempotency_key).
--   * 0008 (esta) -> eliminar a ambiguidade de `submitting -> provedor
--             possivelmente aceitou -> processo falha antes do commit local`.
--
-- Justificativa dos campos novos (requisito 8.0.5):
--   1. `aliexpress_orders.attempt_key` (VARCHAR(64), index)
--        Identificador local estável de TENTATIVA/idempotência para cada
--        submissão lógica. É criado ANTES da chamada lógica ao provider,
--        permanece estável durante a reconciliação da mesma tentativa, não
--        depende do catálogo, não contém segredo e é deterministicamente
--        relacionado à ordem + número da tentativa
--        (`sha256('...:attempt:v1:<order_id>:<seq>')`, seq = attempt_count).
--   (O campo `idempotency_key` existente é POR-ORDEM (identidade do snapshot)
--    e não representa tentativas individuais — por isso um campo novo.)
--   2. Estado `pending_reconciliation` na CHECK de aliexpress_orders
--        Semântica EXPLÍCITA para resultado ambíguo/desconhecido de submissão
--        (timeout/desconexão com resultado remoto desconhecido): a ordem NÃO
--        volta para `ready`, NÃO permite nova criação automática e exige
--        reconciliação explícita sintética (fake/mock/hard-off nesta fase).
--
-- Aplicação (uma ou mais vezes, PostgreSQL):
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0008_aliexpress_submission_reconciliation.sql

\set ON_ERROR_STOP on

-- Restringe toda a migration ao schema dedicado da PolloModas.
SET search_path TO pollomodas;

-- ============================================================
-- 1) Identificador por tentativa
-- ============================================================
ALTER TABLE aliexpress_orders
    ADD COLUMN IF NOT EXISTS attempt_key VARCHAR(64);

CREATE INDEX IF NOT EXISTS ix_aliexpress_orders_attempt_key
    ON aliexpress_orders (attempt_key);

-- ============================================================
-- 2) Novo estado explícito: pending_reconciliation
--    (drop + recreate do CHECK, idempotente)
-- ============================================================
DO $$
BEGIN
    EXECUTE 'ALTER TABLE aliexpress_orders DROP CONSTRAINT IF EXISTS ck_aliexpress_order_state';
END $$;

DO $$
BEGIN
    EXECUTE 'ALTER TABLE aliexpress_orders ADD CONSTRAINT ck_aliexpress_order_state CHECK (
        state IN (
            ''queued'', ''blocked'', ''ready'', ''submitting'', ''submitted'',
            ''confirmed'', ''shipped'', ''delivered'', ''cancel_requested'',
            ''cancelled'', ''failed'', ''manual_review'', ''pending'',
            ''pending_reconciliation''
        )
    )';
END $$;

-- ============================================================
-- Verificação de schema public invariável
-- ============================================================
-- Esta migration NÃO deve criar objetos no schema public.
DO $$
DECLARE
    obj_count integer;
BEGIN
    SELECT count(*) INTO obj_count
    FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name IN ('aliexpress_orders', 'aliexpress_credentials', 'aliexpress_order_items');
    IF obj_count > 0 THEN
        RAISE EXCEPTION 'Migration 0008 criou objetos no schema public (proibido)';
    END IF;
END $$;