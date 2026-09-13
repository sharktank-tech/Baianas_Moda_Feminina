-- Migration 0016: ledger de refund pós-cancelamento (FASE 8.3.0-H)
-- Idempotente, somente schema pollomodas. Não chama gateway financeiro.
\set ON_ERROR_STOP on
SET search_path TO pollomodas;

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS supplier_refund_state VARCHAR(30) NULL,
    ADD COLUMN IF NOT EXISTS supplier_refund_requested_at TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS supplier_refund_confirmed_at TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS supplier_refund_last_error VARCHAR(64) NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_orders_supplier_refund_state'
          AND conrelid = 'pollomodas.orders'::regclass
    ) THEN
        ALTER TABLE orders
            ADD CONSTRAINT ck_orders_supplier_refund_state
            CHECK (
                supplier_refund_state IS NULL OR
                supplier_refund_state IN (
                    'requested', 'pending_webhook', 'confirmed',
                    'failed', 'manual_review'
                )
            );
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'orders'
          AND column_name LIKE 'supplier_refund_%'
    ) THEN
        RAISE EXCEPTION 'supplier refund columns unexpectedly exist in public.orders';
    END IF;
END $$;
