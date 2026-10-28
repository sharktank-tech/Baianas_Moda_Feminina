-- Migration 0015: snapshot financeiro do fornecedor por item (FASE 8.2.0-D)
-- Idempotente, restrita ao schema baianas_moda_feminina. Nunca toca public.
--
-- Campos nullable preservam pedidos legados. Custo desconhecido continua NULL:
-- a aplicação nunca o converte em zero para calcular margem.

\set ON_ERROR_STOP on

SET search_path TO baianas_moda_feminina;

ALTER TABLE order_items
    ADD COLUMN IF NOT EXISTS supplier_unit_cost_snapshot NUMERIC(10, 2) NULL,
    ADD COLUMN IF NOT EXISTS supplier_shipping_cost_snapshot NUMERIC(10, 2) NULL,
    ADD COLUMN IF NOT EXISTS supplier_landed_cost_snapshot NUMERIC(10, 2) NULL,
    ADD COLUMN IF NOT EXISTS supplier_cost_currency_snapshot VARCHAR(3) NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_order_items_supplier_unit_cost_positive'
          AND conrelid = 'baianas_moda_feminina.order_items'::regclass
    ) THEN
        ALTER TABLE order_items ADD CONSTRAINT
            ck_order_items_supplier_unit_cost_positive
            CHECK (
                supplier_unit_cost_snapshot IS NULL
                OR supplier_unit_cost_snapshot > 0
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_order_items_supplier_shipping_nonnegative'
          AND conrelid = 'baianas_moda_feminina.order_items'::regclass
    ) THEN
        ALTER TABLE order_items ADD CONSTRAINT
            ck_order_items_supplier_shipping_nonnegative
            CHECK (
                supplier_shipping_cost_snapshot IS NULL
                OR supplier_shipping_cost_snapshot >= 0
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_order_items_supplier_landed_cost_positive'
          AND conrelid = 'baianas_moda_feminina.order_items'::regclass
    ) THEN
        ALTER TABLE order_items ADD CONSTRAINT
            ck_order_items_supplier_landed_cost_positive
            CHECK (
                supplier_landed_cost_snapshot IS NULL
                OR supplier_landed_cost_snapshot > 0
            );
    END IF;
END $$;

DO $$
DECLARE
    obj_count integer;
BEGIN
    SELECT count(*) INTO obj_count
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'order_items'
      AND column_name IN (
          'supplier_unit_cost_snapshot',
          'supplier_shipping_cost_snapshot',
          'supplier_landed_cost_snapshot',
          'supplier_cost_currency_snapshot'
      );
    IF obj_count > 0 THEN
        RAISE EXCEPTION 'Migration 0015 alterou public.order_items (proibido)';
    END IF;
END $$;
