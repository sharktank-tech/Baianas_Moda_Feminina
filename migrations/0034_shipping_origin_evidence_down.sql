-- Reverte a origem de envio com evidência (Etapa 1).
-- Descarta as classificações/evidências registradas junto com a estrutura e
-- desfaz a seleção de operação nacional. Restringido a baianas_moda_feminina; idempotente.

ALTER TABLE baianas_moda_feminina.product_variants
    DROP COLUMN IF EXISTS national_shipping_operation;

DROP INDEX IF EXISTS baianas_moda_feminina.uq_shipping_origin_cover_all;
DROP INDEX IF EXISTS baianas_moda_feminina.uq_shipping_origin_variant_sku;
DROP TABLE IF EXISTS baianas_moda_feminina.supplier_sku_shipping_origins;
