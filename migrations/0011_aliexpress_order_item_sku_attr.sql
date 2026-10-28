-- Migration 0011: freeze AliExpress sku_attr in fulfillment snapshot (Fase 8.0.10.1)
-- Idempotente, restrita ao schema `baianas_moda_feminina`. Nunca toca `public`.
--
-- Contexto:
--   Depois que AliExpressOrderItem é materializado, catálogo mutável nunca mais
--   pode reconstruir identidade de compra. supplier_sku_attr congela o valor
--   canônico "pid:vid;pid:vid" (sku_property_id: property_value_id ordenado)
--   gerado uma única vez a partir de ProductVariant.attributes.
--   materialize_submission_snapshot() passa a usar exclusivamente este frozen
--   value; nunca relê ProductVariant.attributes.
--
-- Esta migration NÃO habilita HTTP, não altera estados e não faz backfill lendo
-- catálogo (linhas legadas permanecem com supplier_sku_attr NULL e falham
-- closed quando exigido).

\set ON_ERROR_STOP on

SET search_path TO baianas_moda_feminina;

ALTER TABLE aliexpress_order_items
    ADD COLUMN IF NOT EXISTS supplier_sku_attr TEXT;

-- CHECK: supplier_sku_attr IS NULL OR btrim(...) <> '' (não permite string vazia/whitespace)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
          WHERE conname = 'ck_aliexpress_order_item_sku_attr_not_empty'
            AND conrelid = (
                SELECT c.oid FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'baianas_moda_feminina' AND c.relname = 'aliexpress_order_items'
            )
    ) THEN
        ALTER TABLE aliexpress_order_items
            ADD CONSTRAINT ck_aliexpress_order_item_sku_attr_not_empty CHECK (
                supplier_sku_attr IS NULL OR btrim(supplier_sku_attr) <> ''
            );
    END IF;
END $$;

-- Defesa: objetos novos da integração permanecem proibidos em public.
DO $$
DECLARE
    obj_count integer;
BEGIN
    SELECT count(*) INTO obj_count
    FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name IN (
          'aliexpress_credentials',
          'aliexpress_order_items',
          'aliexpress_submission_snapshots'
      );
    IF obj_count > 0 THEN
        RAISE EXCEPTION 'Migration 0011 criou objetos no schema public (proibido)';
    END IF;
END $$;
