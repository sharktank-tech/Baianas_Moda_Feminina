-- Migration 0014: CPF nativo do destinatário no pedido (FASE 8.0.12C)
-- Idempotente, restrita ao schema baianas_moda_feminina. Nunca toca public.
--
-- Pedidos legados permanecem válidos com customer_cpf NULL. Novos checkouts
-- AliExpress validam checksum na aplicação antes da criação da preferência MP.

\set ON_ERROR_STOP on

SET search_path TO baianas_moda_feminina;

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS customer_cpf VARCHAR(11) NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_orders_customer_cpf_length'
          AND conrelid = 'baianas_moda_feminina.orders'::regclass
    ) THEN
        ALTER TABLE orders
            ADD CONSTRAINT ck_orders_customer_cpf_length
            CHECK (customer_cpf IS NULL OR length(customer_cpf) = 11);
    END IF;
END $$;

DO $$
DECLARE
    obj_count integer;
BEGIN
    SELECT count(*) INTO obj_count
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'orders'
      AND column_name = 'customer_cpf';
    IF obj_count > 0 THEN
        RAISE EXCEPTION 'Migration 0014 criou customer_cpf no schema public (proibido)';
    END IF;
END $$;
