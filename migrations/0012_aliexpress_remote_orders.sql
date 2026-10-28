-- Migration 0012: Persist remote order IDs with durable attempts (Fase 8.0.11B)
-- Idempotente, restrita ao schema `baianas_moda_feminina`. Nunca toca `public`.
--
-- Contexto:
--   A submissão AliExpress agora suporta cardinalidade 0/1/N de
--   remote order IDs retornados por aliexpress.trade.buy.placeorder.
--   Duas novas tabelas registram:
--     1. AliExpressSubmissionAttempt — barreira durável antes do HTTP
--     2. AliExpressRemoteOrder — identidades remotas persistidas
--
-- Esta migration NÃO habilita HTTP, não altera estados do parent
-- e não faz backfill lendo catálogo.
--
-- Não adiciona state in_flight/unknown em AliExpressOrder.
-- Não altera o CHECK de estados do parent.

\set ON_ERROR_STOP on

SET search_path TO baianas_moda_feminina;

-- ============================================================
-- 1. Tabela: aliexpress_submission_attempts
-- ============================================================

CREATE TABLE IF NOT EXISTS aliexpress_submission_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    aliexpress_order_id UUID NOT NULL REFERENCES aliexpress_orders(id) ON DELETE CASCADE,
    attempt_key VARCHAR(64) NOT NULL,
    state VARCHAR(20) NOT NULL,
    started_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP NULL,
    last_error TEXT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT ck_attempt_state CHECK (state IN ('in_flight','confirmed','rejected','unknown'))
);

CREATE INDEX IF NOT EXISTS idx_ase_aliexpress_order_id ON aliexpress_submission_attempts(aliexpress_order_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ase_attempt_key ON aliexpress_submission_attempts(attempt_key);

-- ============================================================
-- 2. Tabela: aliexpress_remote_orders
-- ============================================================

CREATE TABLE IF NOT EXISTS aliexpress_remote_orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    aliexpress_order_id UUID NOT NULL REFERENCES aliexpress_orders(id) ON DELETE CASCADE,
    remote_order_id VARCHAR(64) NOT NULL,
    position INTEGER NOT NULL CONSTRAINT ck_position_nonneg CHECK (position >= 0),
    attempt_key VARCHAR(64) NULL,
    remote_status TEXT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT uq_remote_order_id UNIQUE (remote_order_id),
    CONSTRAINT uq_order_position UNIQUE (aliexpress_order_id, position)
);

CREATE INDEX IF NOT EXISTS idx_areo_aliexpress_order_id ON aliexpress_remote_orders(aliexpress_order_id);
CREATE INDEX IF NOT EXISTS idx_areo_attempt_key ON aliexpress_remote_orders(attempt_key);

-- ============================================================
-- 3. Backfill: registros legados com ali_order_id != NULL
-- ============================================================

-- Inserir filho para cada aliexpress_order com ali_order_id != NULL
-- Preservar parent.ali_order_id, attempt_key, ali_status
-- Não criar SubmissionAttempt para legado
-- Idempotente: verificar se já existe child antes de inserir

INSERT INTO aliexpress_remote_orders (
    id, aliexpress_order_id, remote_order_id, position,
    attempt_key, remote_status, created_at, updated_at
)
SELECT
    gen_random_uuid(),
    ao.id,
    ao.ali_order_id,
    0,
    ao.attempt_key,
    ao.ali_status,
    now(),
    now()
FROM aliexpress_orders ao
WHERE ao.ali_order_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM aliexpress_remote_orders aro
    WHERE aro.aliexpress_order_id = ao.id
      AND aro.position = 0
      AND aro.remote_order_id = ao.ali_order_id
  );

-- ============================================================
-- 4. Validações
-- ============================================================

-- Verificar que não há tabelas AliExpress em public schema
DO $$
DECLARE
    obj_count INTEGER;
BEGIN
    SELECT count(*) INTO obj_count
    FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name IN (
          'aliexpress_submission_attempts',
          'aliexpress_remote_orders'
      );
    IF obj_count > 0 THEN
        RAISE EXCEPTION 'Prohibited AliExpress tables found in public schema (%)', obj_count;
    END IF;
END $$;
