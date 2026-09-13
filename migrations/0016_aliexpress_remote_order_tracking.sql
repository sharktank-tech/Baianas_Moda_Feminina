-- Migration 0016: tracking sanitizado por pedido remoto (FASE 8.3.0-B)
-- Idempotente e restrita ao schema pollomodas. Não habilita HTTP ou polling.
\set ON_ERROR_STOP on

SET search_path TO pollomodas;

ALTER TABLE aliexpress_remote_orders
    ADD COLUMN IF NOT EXISTS tracking_code VARCHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS carrier VARCHAR(64) NULL,
    ADD COLUMN IF NOT EXISTS tracking_updated_at TIMESTAMP NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_areo_tracking_code_format'
          AND conrelid = 'pollomodas.aliexpress_remote_orders'::regclass
    ) THEN
        ALTER TABLE aliexpress_remote_orders
            ADD CONSTRAINT ck_areo_tracking_code_format
            CHECK (
                tracking_code IS NULL OR
                tracking_code ~ '^[A-Z0-9][A-Z0-9-]{4,63}$'
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_areo_carrier_format'
          AND conrelid = 'pollomodas.aliexpress_remote_orders'::regclass
    ) THEN
        ALTER TABLE aliexpress_remote_orders
            ADD CONSTRAINT ck_areo_carrier_format
            CHECK (
                carrier IS NULL OR
                carrier ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
            );
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'aliexpress_remote_orders'
          AND column_name IN ('tracking_code', 'carrier', 'tracking_updated_at')
    ) THEN
        RAISE EXCEPTION 'tracking columns unexpectedly exist in public schema';
    END IF;
END $$;
