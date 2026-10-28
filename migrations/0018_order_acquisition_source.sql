-- Migration 0018: origem de tráfego no pedido (FASE 9.0.0)
-- Idempotente, somente schema baianas_moda_feminina. Captura sanitizada e opcional;
-- pedidos legados permanecem NULL (fail-closed em métricas).
\set ON_ERROR_STOP on
SET search_path TO baianas_moda_feminina;

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS acquisition_source VARCHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS acquisition_medium VARCHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS acquisition_campaign VARCHAR(120) NULL,
    ADD COLUMN IF NOT EXISTS acquisition_referrer VARCHAR(120) NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'orders'
          AND column_name LIKE 'acquisition_%'
    ) THEN
        RAISE EXCEPTION 'acquisition columns unexpectedly exist in public.orders';
    END IF;
END $$;