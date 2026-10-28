-- Additive migration: unknown historical taxes remain NULL. No backfill,
-- repricing, credentials, API grants or operational gates are changed.
ALTER TABLE baianas_moda_feminina.supplier_products
    ADD COLUMN IF NOT EXISTS import_duty numeric(10,2),
    ADD COLUMN IF NOT EXISTS icms numeric(10,2);
ALTER TABLE baianas_moda_feminina.supplier_reference_freight_quotes
    ADD COLUMN IF NOT EXISTS supplier_unit_cost numeric(10,2),
    ADD COLUMN IF NOT EXISTS import_duty numeric(10,2),
    ADD COLUMN IF NOT EXISTS icms numeric(10,2);
ALTER TABLE baianas_moda_feminina.order_items
    ADD COLUMN IF NOT EXISTS supplier_import_duty_snapshot numeric(10,2),
    ADD COLUMN IF NOT EXISTS supplier_icms_snapshot numeric(10,2),
    ADD COLUMN IF NOT EXISTS supplier_cost_evidence_snapshot json;

ALTER TABLE baianas_moda_feminina.supplier_products
    DROP CONSTRAINT IF EXISTS ck_supplier_product_purchase_taxes;
ALTER TABLE baianas_moda_feminina.supplier_products
    ADD CONSTRAINT ck_supplier_product_purchase_taxes CHECK (
      (import_duty IS NULL AND icms IS NULL) OR
      (import_duty IS NOT NULL AND icms IS NOT NULL AND
       import_duty BETWEEN 0 AND 99999999.99 AND icms BETWEEN 0 AND 99999999.99)
    );
ALTER TABLE baianas_moda_feminina.supplier_reference_freight_quotes
    DROP CONSTRAINT IF EXISTS ck_supplier_quote_purchase_costs;
ALTER TABLE baianas_moda_feminina.supplier_reference_freight_quotes
    ADD CONSTRAINT ck_supplier_quote_purchase_costs CHECK (
      (supplier_unit_cost IS NULL AND import_duty IS NULL AND icms IS NULL) OR
      (supplier_unit_cost IS NOT NULL AND import_duty IS NOT NULL AND icms IS NOT NULL AND
       supplier_unit_cost > 0 AND supplier_unit_cost <= 99999999.99 AND
       import_duty BETWEEN 0 AND 99999999.99 AND icms BETWEEN 0 AND 99999999.99 AND
       quantity = 1 AND qty_max IS NOT NULL AND qty_max = 1 AND
       dest_cep IS NOT NULL AND evidence_ref_hash IS NOT NULL)
    );
ALTER TABLE baianas_moda_feminina.order_items
    DROP CONSTRAINT IF EXISTS ck_order_items_purchase_taxes;
ALTER TABLE baianas_moda_feminina.order_items
    ADD CONSTRAINT ck_order_items_purchase_taxes CHECK (
      (supplier_import_duty_snapshot IS NULL AND supplier_icms_snapshot IS NULL) OR
      (supplier_import_duty_snapshot IS NOT NULL AND supplier_icms_snapshot IS NOT NULL AND
       supplier_import_duty_snapshot BETWEEN 0 AND 99999999.99 AND
       supplier_icms_snapshot BETWEEN 0 AND 99999999.99 AND
       supplier_unit_cost_snapshot IS NOT NULL AND
       supplier_shipping_cost_snapshot IS NOT NULL AND
       supplier_landed_cost_snapshot IS NOT NULL AND
       supplier_landed_cost_snapshot = supplier_unit_cost_snapshot +
         supplier_shipping_cost_snapshot + supplier_import_duty_snapshot + supplier_icms_snapshot)
    );
