-- Migration 0019: recibo de recuperação de checkout (FASE 9.0.0-C)
-- Idempotente, somente schema baianas_moda_feminina. Marca quando o lembrete de retomada
-- de pagamento foi enviado (dedupe); pedidos sem envio permanecem NULL.
\set ON_ERROR_STOP on
SET search_path TO baianas_moda_feminina;

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS recovery_email_sent_at TIMESTAMP NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'orders'
          AND column_name LIKE 'recovery_email_%'
    ) THEN
        RAISE EXCEPTION 'recovery_email columns unexpectedly exist in public.orders';
    END IF;
END $$;